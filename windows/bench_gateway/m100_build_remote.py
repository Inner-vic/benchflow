"""One fixed 2737 build, in an isolated source copy/container; workstation only."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda: f.read(1048576), b''):
            h.update(part)
    return h.hexdigest()


def capture(argv, timeout=60):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=timeout).stdout.strip()


def main():
    directory = Path(__file__).resolve().parent
    assert directory.parent.name == 'builds' and directory.parent.parent.name.startswith('u_')
    assert not (directory / 'status.json').exists(), 'existing build must be inspected, never retried in place'
    request = json.loads((directory / 'request.json').read_text())
    # Only the installed PC-side catalog supplies this validated recipe.
    build=request['identity']['build']
    SOURCE=Path(build['source']);COMMIT=build['commit'];DIFF=build['source_diff_sha256']
    LOCK=build['source_lock_sha256'];IMAGE=build['image'];CACHE=build['cache']
    limit = request['build_seconds']
    assert type(limit) is int and 30 <= limit <= 300
    started = time.time()
    container = 'gateway-m100-' + directory.name
    receipt = {'state': 'PREPARING', 'started_at': started, 'source': str(SOURCE),
               'commit': COMMIT, 'source_diff_sha256': DIFF, 'source_lock_sha256': LOCK,
               'image_id': IMAGE, 'container': container, 'network': 'none',
               'bench_actions': 'NOT_RUN', 'source_modified': False}

    def state(**values):
        receipt.update(values)
        temp = directory / 'status.tmp'
        temp.write_text(json.dumps(receipt, indent=2) + '\n')
        os.replace(temp, directory / 'status.json')

    made_container = False
    try:
        state()
        assert capture(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD']) == COMMIT
        diff = capture(['git', '-C', str(SOURCE), 'diff', '--binary'])
        assert hashlib.sha256(diff.encode()).hexdigest() == DIFF
        assert sha(SOURCE / 'build/conan.lock') == LOCK
        assert capture(['docker', 'image', 'inspect', '--format', '{{.Id}}', IMAGE]) == IMAGE
        mem = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        assert int(mem['MemAvailable'].strip().split()[0]) > 10 * 1024 * 1024, 'less than 10GiB available memory'
        assert shutil.disk_usage(directory).free > 10 * 1024**3
        target = directory / 'source'
        shutil.copytree(SOURCE, target, symlinks=True,
                        ignore=lambda path, names: {'build', 'install'} if Path(path) == SOURCE else set())
        (target / 'build').mkdir()
        shutil.copy2(SOURCE / 'build/conan.lock', target / 'build/conan.lock')
        settings=base64.b64decode(request['identity']['conan_settings'],validate=True)
        assert hashlib.sha256(settings).hexdigest()==build['settings_sha256'],'Conan schema differs'
        (directory / 'settings.yml').write_bytes(settings)
        capture(['docker', 'run', '--detach', '--name', container, '--network=none',
                 '--cpus=4', '--memory=10g', '--pids-limit=512',
                 '-e', 'CONAN_CPU_COUNT=4', '-e', 'CMAKE_BUILD_PARALLEL_LEVEL=4',
                 '--mount', 'type=bind,src=' + str(target) + ',dst=/root/workspace/apps',
                 '--mount', 'type=bind,src=' + CACHE + ',dst=/root/.conan/data',
                 '--mount', 'type=bind,src=' + str(directory / 'settings.yml') + ',dst=/root/.conan/settings.yml,readonly',
                 IMAGE, 'sleep', 'infinity'])
        made_container = True
        state(state='BUILDING', compile_started_at=time.time(), copied_source=str(target))
        argv = [sys.executable, str(directory / 'build_uroad_eval.py'), '--source', str(target),
                '--expected-commit', COMMIT, '--artifact-id', 'artifact',
                '--artifacts-root', str(directory), '--container', container,
                '--offline-lock', '--execute']
        with (directory / 'coordinator.log').open('w') as log:
            result = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, timeout=limit)
        assert result.returncode == 0, 'build helper failed; inspect artifact/build.log and coordinator.log'
        manifest = json.loads((directory / 'artifact/build-manifest.json').read_text())
        assert manifest['state'] == 'BUILT'
        output = Path(manifest['output'])
        assert sha(output) == manifest['sha256'] and output.stat().st_size == manifest['size_bytes']
        state(state='BUILT', finished_at=time.time(), elapsed_seconds=round(time.time()-started, 2),
              output=str(output), sha256=manifest['sha256'], size_bytes=manifest['size_bytes'],
              machine=manifest['machine'], manifest=str(directory / 'artifact/build-manifest.json'))
    except Exception as exc:
        state(state='BUILD_FAILED', finished_at=time.time(), elapsed_seconds=round(time.time()-started, 2),
              error_type=type(exc).__name__, error=str(exc)[:1000])
    finally:
        if made_container:
            stopped = subprocess.run(['docker', 'stop', '--time=10', container], capture_output=True, timeout=30)
            state(container_stopped=stopped.returncode == 0)
        state(cleanup_finished=True)


if __name__ == '__main__':
    main()
