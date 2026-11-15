"""Discover and evaluate every scene in a workstation prediction product by default."""
import argparse
import base64
import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

from runtime_support import load_runtime
from shared_runtime import contract


def bootstrap(request, pin):
    scripts = Path(__file__).resolve().parent
    scope = (scripts / 'report_scope.py').read_text(encoding='utf-8')
    remote = (scripts / 'directory_worker.py').read_text(encoding='utf-8')
    return ('import sys,types\n'
            'scope_module=types.ModuleType("report_scope")\n'
            'sys.modules["report_scope"]=scope_module\n'
            'exec(' + repr(scope) + ',scope_module.__dict__)\n' + remote +
            '\nmain(' + repr(request) + ',' + repr(pin) + ')\n')


def remote_call(request, pin, alias, progress_path=None):
    if not re.fullmatch(r'[A-Za-z0-9._-]+', alias):
        raise ValueError('unsafe workstation alias')
    argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'ForwardAgent=no', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=10', alias, 'python3 -B -']
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding='utf-8')
    # stderr is SSH diagnostics only; remote run.sh output is written to its isolated run directory.
    process.stdin.write(bootstrap(request, pin))
    process.stdin.close()
    result = None
    log = progress_path.open('a', encoding='utf-8') if progress_path else None
    try:
        for line in process.stdout:
            if len(line) > 40 * 1024 * 1024:
                raise ValueError('unexpectedly large remote response')
            value = json.loads(line)
            if value.get('type') == 'progress':
                if log:
                    log.write(json.dumps(value, ensure_ascii=False) + '\n')
                    log.flush()
                print(json.dumps(value, ensure_ascii=False), flush=True)
            elif value.get('type') in ('result', 'error'):
                result = value
            else:
                raise ValueError('unexpected remote protocol response')
        stderr = process.stderr.read()
        code = process.wait()
        if code or not result or result.get('type') == 'error':
            raise ValueError((result or {}).get('message') or stderr[-3000:] or 'SSH returned no result; inspect existing run before retry')
        return result
    finally:
        if log:
            log.close()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def metadata_for(receipt):
    plan = receipt['discovery']
    app = plan.get('app_artifact', {})
    library = plan.get('library_provenance', {})
    app_name = re.split(r'[/\\]', app.get('path', ''))[-1]
    library_name = re.split(r'[/\\]', library.get('source', ''))[-1]
    metadata = {
        'run_id': receipt['run_id'], 'evaluated_at': receipt['evaluated_at'],
        'scene': '全部%d个场景' % plan['scene_count'] if plan['selection'] == 'all' else '明确选择的%d个场景' % plan['scene_count'],
        'scene_count': plan['scene_count'], 'scenes': plan['scenes'],
        'scene_coverage': receipt['scene_coverage'],
        'evaluation_set_id': contract()['evaluator']['version'],
        'requested_tasks': plan['required_tasks'], 'task_switches': plan['task_switches'],
        'evaluation_rules': '固定评测包原生全量统计，max_scenes_for_debug=0；任务开关和阈值保持模板原值。缺少GT的任务明确保留缺口，不作完整通过结论。',
        'prediction_path': plan['prediction_path'], 'prediction_identity': plan['prediction_receipt_sha256'],
        'truth_root': plan['truth_root'], 'config_path': receipt['config_path'],
        'dataset_identity': receipt['dataset_identity'], 'evaluator_identity': receipt['package_manifest_sha256'],
        'task_config_identity': receipt['config_sha256'],
        'aggregation': {'method': 'native_evaluator_full_batch', 'scene_percentages_averaged': False,
                        'max_scenes_for_debug': 0, 'selection': plan['selection']},
        'source_provenance': {k: plan[k] for k in ('producer_run_id', 'job_path', 'job_sha256',
                               'prepared_manifest', 'prepared_manifest_sha256', 'app_artifact', 'library_provenance')},
        'title': '%s｜%d场景台架评测报告' % (app_name or '版本待核', plan['scene_count']),
    }
    if app_name:
        metadata['version'] = app_name + (' / 替换 ' + library_name if library_name else '')
        metadata['app_tag'] = app.get('path', app_name) + (' / library SHA-256 ' + library['sha256'] if library.get('sha256') else '')
    return metadata


def run(prediction, *, run_dir=None, scenes=None, required_tasks=None, execute=False,
        collect_only=False, alias=None, call=remote_call):
    pin = contract()
    alias = alias or pin['workstation_alias']
    request = {'action': 'collect' if collect_only else ('execute' if execute else 'discover'),
               'prediction_path': prediction, 'scenes': scenes, 'required_tasks': required_tasks}
    if execute or collect_only:
        if run_dir is None:
            raise ValueError('--run-dir is required for execution/collection')
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        request_path = run_dir / 'directory-request.json'
        if request_path.exists():
            previous = json.loads(request_path.read_text(encoding='utf-8'))
            if any(request[k] != previous[k] for k in ('prediction_path', 'scenes', 'required_tasks')):
                raise ValueError('local run directory belongs to another source or selection')
            request['run_id'] = previous['run_id']
        elif collect_only:
            raise ValueError('collection needs the preserved directory-request.json from this attempt')
        else:
            request['run_id'] = 'directory-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
            save(request_path, request)
    payload = call(request, pin, alias, run_dir / 'progress.jsonl' if execute or collect_only else None)
    if not execute and not collect_only:
        return payload
    raw = base64.b64decode(payload.pop('report_base64'), validate=True)
    receipt = payload['receipt']
    if hashlib.sha256(raw).hexdigest() != receipt['report_sha256']:
        raise ValueError('collected raw report hash mismatch')
    report = run_dir / 'evaluation-report.txt'
    if report.exists() and report.read_bytes() != raw:
        raise ValueError('local report changed; refusing overwrite')
    report.write_bytes(raw)
    metadata = metadata_for(receipt)
    parsed = load_runtime('parse_eval_report').parse_report(report)
    validity = load_runtime('parse_eval_report').validate_result(parsed, {'required_tasks': metadata['requested_tasks']})
    complete = all(r['completed_frames'] == r['truth_frames'] for r in receipt['scene_coverage'])
    state = 'EVALUATION_COMPLETED' if validity['decision'] == 'VALID' and complete else 'EVALUATION_PARTIAL'
    summary = {**payload, 'state': state, 'validity': validity, 'metrics': parsed['metrics'],
               'scene_count': receipt['scene_count'], 'local_report_path': str(report.resolve()),
               'metadata_path': str((run_dir / 'report-metadata.json').resolve()),
               'partial_report_publishable': True, 'upload_required': False, 'feishu_identity': 'user'}
    save(run_dir / 'directory-receipt.json', receipt)
    save(run_dir / 'report-metadata.json', metadata)
    save(run_dir / 'evaluation-summary.json', summary)
    return summary


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--prediction', required=True, help='shared workstation product path; all scenes by default')
    cli.add_argument('--run-dir', type=Path)
    cli.add_argument('--scene', action='append', help='only use when the human explicitly selects a subset')
    cli.add_argument('--required-task', choices=['trajectory', 'vision'], action='append')
    cli.add_argument('--alias')
    mode = cli.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true')
    mode.add_argument('--collect-only', action='store_true')
    args = cli.parse_args()
    result = run(args.prediction, run_dir=args.run_dir, scenes=args.scene, required_tasks=args.required_task,
                 execute=args.execute, collect_only=args.collect_only, alias=args.alias)
    print(json.dumps({k: v for k, v in result.items() if k not in ('receipt',)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'state': 'DIRECTORY_EVALUATION_BLOCKED', 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
