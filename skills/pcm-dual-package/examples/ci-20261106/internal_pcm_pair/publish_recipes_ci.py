"""Publish only the reviewed PCM recipes, inside the original project's CI."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request

PROJECT = 'livc-acore/chassis/uroad'
BRANCH = 'codex/pcm-dual-publish-20261106'
SOURCE = '9a61fe17167b97d7b13c5b0c02348762e84ed206'
XLIDAR_SOURCE = '9ae4cb4d3efd1dd334b5e04203f1087b36867b04'
PAIR = 'dev314-20261027-001'
REMOTE = 'liconan-dev-local'
ORIGIN = 'https://artifactory.ep.chehejia.com/artifactory/'
EXPECTED = {
    'OFF': ('uroad_m100/3.1.4-pcm-off.' + PAIR + '@livc/pcm-' + PAIR,
            '457e7b36d19fc5c29bd48a640ccfd2bd05e561833feeb5e7350f20a6ff9047fb'),
    'ON': ('uroad_m100/3.1.4-pcm-on.' + PAIR + '@livc/pcm-' + PAIR,
           '991c59684d944d623f4e90b5e7511395e431a30a053e644b2f2908bc0bebe7e8'),
    'xlidar': ('xlidar/0.2.8-pcm.' + PAIR + '@livc/pcm-' + PAIR,
               'c58056601bf55359d7968fb48620f9df64a5c195145aba2b0ecb223bad584cd8'),
}


def check_environment(env):
    if env.get('GITLAB_CI') != 'true' or env.get('CI_PROJECT_PATH') != PROJECT:
        raise ValueError('Run only in the maintained uroad project CI, never in a personal fork')
    if env.get('CI_COMMIT_BRANCH') != BRANCH or env.get('CI_COMMIT_TAG'):
        raise ValueError('Run only on the isolated PCM maintenance branch')
    username = env.get('ARTIFACTORY_CONAN_USER', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*-rt-svc', username):
        raise ValueError('NEEDS_PROJECT_SERVICE_ACCOUNT: a CI-managed *-rt-svc account is required')
    if not env.get('ARTIFACTORY_CONAN_PASS'):
        raise ValueError('NEEDS_PROJECT_SERVICE_ACCOUNT: the existing CI credential is unavailable')
    return username


def reviewed_inputs(folder):
    manifest = json.loads((folder / 'components.json').read_text(encoding='utf-8'))
    if manifest['pair_id'] != PAIR or manifest['source_commit'] != SOURCE or manifest['xlidar_commit'] != XLIDAR_SOURCE:
        raise ValueError('Unexpected source identity')
    if set(manifest['components']) != set(EXPECTED):
        raise ValueError('Unexpected component scope')
    for mode, item in manifest['components'].items():
        expected_ref, expected_sha = EXPECTED[mode]
        raw = (folder / mode / 'conanfile.py').read_bytes()
        if item['ref'] != expected_ref or item['recipe_sha256'] != expected_sha:
            raise ValueError('Unexpected publication reference or checksum')
        if hashlib.sha256(raw).hexdigest() != expected_sha:
            raise ValueError('Recipe differs from reviewed bytes')
    return manifest


def remote_path(ref):
    identity, owner_channel = ref.split('@')
    name, version = identity.split('/')
    owner, channel = owner_channel.split('/')
    return REMOTE + '/' + '/'.join([owner, name, version, channel]) + '/'


def existing_action(listing, revision, digest, fetch_recipe):
    """An existing version is read-only: identical -> skip; any conflict -> stop."""
    if listing is None:
        return 'CREATE_NEW'
    revisions = [c['uri'].strip('/') for c in listing.get('children', []) if c.get('folder')]
    if revisions != [revision]:
        raise ValueError('Existing private version is incomplete or has a different revision; refusing overwrite')
    if hashlib.sha256(fetch_recipe(revision)).hexdigest() != digest:
        raise ValueError('Existing recipe content differs; refusing overwrite')
    return 'READ_BACK_AND_SKIP'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Unexpected redirect; credentials not forwarded')


def main():
    receipt = {'status': 'PREFLIGHT', 'published': [], 'verified': [], 'job_triggered': False,
               'existing_versions_overwritten': False, 'shared_pipeline_changed': False}
    receipt_path = Path('pcm-publication-receipt.json')

    def save():
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    try:
        username = check_environment(os.environ)
        secret = os.environ['ARTIFACTORY_CONAN_PASS']
        receipt['account'] = username
        folder = Path('internal_pcm_pair/recipes')
        manifest = reviewed_inputs(folder)
        changes = subprocess.check_output(['git', 'diff', '--name-only', SOURCE, 'HEAD'], text=True).splitlines()
        if any(p != '.gitlab-ci.yml' and not p.startswith('internal_pcm_pair/') for p in changes):
            raise ValueError('Maintenance branch changes business files')
        authorization = 'Basic ' + base64.b64encode((username + ':' + secret).encode()).decode()
        opener = urllib.request.build_opener(NoRedirect())

        def read(path):
            request = urllib.request.Request(ORIGIN + path, headers={'Authorization': authorization})
            with opener.open(request, timeout=40) as response:
                return response.read(4 * 1024 * 1024)

        def listing(path):
            try:
                return json.loads(read('api/storage/' + path))
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    return None
                raise

        # Private CI cache only; credentials are supplied by existing CI variables.
        env = dict(os.environ, CONAN_USER_HOME=str(Path('.pcm-conan').resolve()),
                   CONAN_NON_INTERACTIVE='1', CONAN_REVISIONS_ENABLED='1',
                   CONAN_LOGIN_USERNAME=username, CONAN_PASSWORD=secret)

        def command(args, phase):
            proc = subprocess.run(args, env=env, stdin=subprocess.DEVNULL, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
            if proc.returncode:
                safe = proc.stdout.replace(secret, '[REDACTED]').replace(authorization, '[REDACTED]')
                safe = '\n'.join(line for line in safe.splitlines()
                                 if not re.search(r'(?i)password|token|secret|authorization|bearer', line))
                receipt.update(failed_phase=phase, diagnostic=safe[-2500:])
                raise RuntimeError(phase + ' failed')

        command(['conan', 'remote', 'add', REMOTE, ORIGIN + 'api/conan/' + REMOTE], 'register-remote')
        command(['conan', 'user', username, '-p', '-r', REMOTE], 'login')
        entries = []
        for mode, item in manifest['components'].items():
            command(['conan', 'export', str(folder / mode), item['ref']], 'export-' + mode)
            identity, owner_channel = item['ref'].split('@')
            name, version = identity.split('/')
            owner, channel = owner_channel.split('/')
            cache = Path(env['CONAN_USER_HOME']) / '.conan/data' / name / version / owner / channel
            revision = json.loads((cache / 'metadata.json').read_text())['recipe']['revision']
            if hashlib.sha256((cache / 'export/conanfile.py').read_bytes()).hexdigest() != item['recipe_sha256']:
                raise ValueError('Conan export changed reviewed recipe')
            path = remote_path(item['ref'])
            action = existing_action(listing(path), revision, item['recipe_sha256'],
                                     lambda rev: read(path + rev + '/export/conanfile.py'))
            entries.append((mode, item, revision, path, action))
        for mode, item, revision, path, action in entries:
            if action == 'CREATE_NEW':
                # Recheck before each upload; never republish an existing version.
                action = existing_action(listing(path), revision, item['recipe_sha256'],
                                         lambda rev: read(path + rev + '/export/conanfile.py'))
                if action == 'CREATE_NEW':
                    command(['conan', 'upload', item['ref'], '-r', REMOTE, '--confirm'], 'upload-' + mode)
                    receipt['published'].append(item['ref'])
            actual = read(path + revision + '/export/conanfile.py')
            if hashlib.sha256(actual).hexdigest() != item['recipe_sha256']:
                raise ValueError('Remote read-back differs from reviewed recipe')
            receipt['verified'].append({'mode': mode, 'ref': item['ref'], 'recipe_revision': revision,
                                        'recipe_sha256': item['recipe_sha256'], 'action': action})
            receipt['status'] = 'PARTIAL'
            save()
        receipt['status'] = 'THREE_REVIEWED_RECIPES_VERIFIED'
        save()
        print(json.dumps({'status': receipt['status'], 'verified_count': len(receipt['verified'])}))
        return 0
    except Exception as error:
        receipt.update(status='STOPPED', error_type=type(error).__name__)
        if isinstance(error, (ValueError, RuntimeError)):
            receipt['reason'] = str(error)
        if isinstance(error, urllib.error.HTTPError):
            receipt['http_status'] = error.code
        save()
        print(json.dumps({k: receipt[k] for k in ['status', 'error_type', 'reason', 'http_status'] if k in receipt}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
