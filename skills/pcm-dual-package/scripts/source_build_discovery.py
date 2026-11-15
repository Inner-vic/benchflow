"""Read-only source/baseline discovery. Never compile, publish, submit CI or use hardware."""
import argparse
import ast
import copy
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import time
import tarfile
from urllib.parse import quote, urlencode, urlsplit
import urllib.error
import urllib.request

from resolve_pcm_input import resolve
from pcm_existing_job import JOB_URL

GITLAB = 'https://gitlab.chehejia.com/api/v4/'
ARTIFACT = 'https://artifactory.ep.chehejia.com/artifactory/'
RELEASE = ARTIFACT + 'api/storage/xcu-acore-test-local/Acore/Civu/Release/'
CORE = ('infer_expert_sdk', 'function-message', 'fundamental-message', 'xcamera', 'xlidar')
REF = re.compile(r'([A-Za-z0-9_-]+)/([A-Za-z0-9_.+-]+)@([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:#([a-f0-9]{1,64}))?\Z')
RANGE = re.compile(r'([A-Za-z0-9_-]+)/\[([^\]]+)\]@([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\Z')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def fingerprint(value):
    return sha(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode())


def range_clauses(value):
    """Bounded subset: numeric stable versions and ANDed comparisons only.

    Conan-specific wildcards, OR, caret, tilde and prerelease comparisons need a
    real Conan resolution receipt; this helper must not approximate them.
    """
    match = RANGE.fullmatch(value)
    if not match:
        return None
    parts = [p.strip() for p in match[2].split(',')]
    options = parts[1:]
    if any(p not in ('include_prerelease=True', 'include_prerelease=False') for p in options):
        return None
    clauses = re.findall(r'(>=|<=|>|<|=)(v?[0-9]+(?:\.[0-9]+)*)', parts[0])
    if not clauses or ''.join(op+v for op, v in clauses) != re.sub(r'\s+', '', parts[0]):
        return None
    return clauses


def satisfies(requirement, actual):
    if REF.fullmatch(requirement):
        return requirement.split('#')[0] == actual.split('#')[0]
    expected, observed = RANGE.fullmatch(requirement), REF.fullmatch(actual)
    clauses = range_clauses(requirement)
    if not expected or not observed or not clauses or (expected[1], expected[3], expected[4]) != (observed[1], observed[3], observed[4]):
        return False
    if not re.fullmatch(r'v?[0-9]+(?:\.[0-9]+)*', observed[2]):
        return False
    def numeric(value):
        return tuple(int(x) for x in value.lstrip('v').split('.'))
    left = numeric(observed[2])
    for operator, bound in clauses:
        right = numeric(bound)
        width = max(len(left), len(right))
        a, b = left+(0,)*(width-len(left)), right+(0,)*(width-len(right))
        if not {'>': a>b, '<': a<b, '>=': a>=b, '<=': a<=b, '=': a==b}[operator]:
            return False
    return True


def declarations(recipe):
    """Inspect syntax only: do not execute Conan recipes or interpolate expressions."""
    found = {name: set() for name in CORE}
    for node in ast.walk(ast.parse(recipe)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'requires' and node.args):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            value = arg.value
        elif isinstance(arg, ast.JoinedStr) and all(isinstance(x, ast.Constant) for x in arg.values):
            value = ''.join(x.value for x in arg.values)
        else:
            continue
        name = value.split('/', 1)[0]
        if name in found:
            found[name].add(value)
    exact, unresolved = {}, {}
    for name, values in found.items():
        if len(values) == 1 and (REF.fullmatch(next(iter(values))) or range_clauses(next(iter(values)))):
            exact[name] = next(iter(values)).split('#')[0]
        else:
            unresolved[name] = sorted(values)
    return exact, unresolved


def source_mode(recipe, cmake):
    values = []
    for node in ast.walk(ast.parse(recipe)):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                   and t.slice.value == 'WITH_EVAL_DUMP' for t in node.targets):
            continue
        if (not isinstance(node.value, ast.Constant)
                or not (type(node.value.value) is bool or (isinstance(node.value.value, str) and node.value.value in ('ON', 'OFF')))):
            return None
        values.append('ON' if node.value.value in (True, 'ON') else 'OFF')
    if values:
        return values[0] if len(values) == 1 else None
    active = '\n'.join(line.split('#', 1)[0] for line in cmake.splitlines())
    if re.search(r'\bset\s*\(\s*WITH_EVAL_DUMP\b', active, re.I):
        return None
    defaults = re.findall(r'\boption\s*\(\s*WITH_EVAL_DUMP\s+"[^"\n]*"\s+(ON|OFF)\s*\)', active, re.I)
    return defaults[0].upper() if len(defaults) == 1 else None


def parameters(build):
    result = {}
    for action in build.get('actions', []):
        for item in action.get('parameters', []):
            name = item.get('name')
            if name in ('CONTROLLER', 'BASE_LINE', 'SOFTWARE_BRANCH', 'BUILDVARIANT',
                        'MANIFEST_NAME', 'APP_VERSION', 'LOCKED_LIBS'):
                if name in result:
                    raise ValueError('Duplicate Jenkins parameter')
                result[name] = item.get('value')
    return result


def host_refs(lock):
    result = {}
    for node in lock['graph_lock']['nodes'].values():
        value = node.get('ref', '')
        if node.get('context', 'host') != 'host' or not value:
            continue
        name = value.split('/', 1)[0]
        if name in result and result[name] != value:
            raise ValueError('Conflicting host dependency in baseline lock')
        result[name] = value
    return result


def storage_uri(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname != 'artifactory.ep.chehejia.com'
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        return None
    canonical = 'https://artifactory.ep.chehejia.com' + parsed.path
    return canonical if canonical.startswith(RELEASE) else None


def discover(request, reader, *, max_lock_checks=8):
    """reader.get(url,limit) returns bytes. All live requests are authenticated GETs."""
    selected = resolve(request.get('repository', 'uroad'), 'repository', request.get('branch'))
    result = {'schema': 'pcm-source-discovery/v1', 'input': selected, 'state': selected['status'],
              'jenkins_submitted': False, 'bench_tasks': 0}
    if selected['status'] != 'READY_TO_PIN_COMMIT':
        return result
    def raw(url, limit=4 * 1024 * 1024):
        return reader.get(url, limit)
    def document(url):
        return json.loads(raw(url))
    project = urlsplit(selected['repository']).path.strip('/')[:-4]
    api = GITLAB + 'projects/' + quote(project, safe='')
    branch = document(api + '/repository/branches/' + quote(selected['branch'], safe=''))
    commit = branch['commit']['id']
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('Unverified source commit')
    def source_file(name):
        return raw(api + '/repository/files/' + quote(name, safe='') + '/raw?ref=' + commit)
    recipe, cmake = source_file('conanfile.py'), source_file('CMakeLists.txt')
    exact, unresolved = declarations(recipe.decode())
    overrides = {}
    for value in request.get('component_overrides', []):
        match = REF.fullmatch(value) if isinstance(value, str) else None
        if not match or match[5] or match[1] in overrides:
            raise ValueError('Component overrides must be unique exact refs')
        overrides[match[1]] = value
    result.update(source={'repository': selected['repository'], 'branch': selected['branch'], 'commit': commit},
                  source_files={'conanfile.py': sha(recipe), 'CMakeLists.txt': sha(cmake)},
                  dependency_requirements=exact, unresolved_dependencies=unresolved,
                  component_overrides=overrides,
                  compatibility='DECLARATIONS_ONLY_NOT_COMPILE_OR_RUNTIME_VALIDATED')
    mode = request.get('base_app_mode')
    if mode is not None and mode not in ('OFF', 'ON'):
        raise ValueError('Invalid explicit base APP mode')
    origin = 'explicit_build_variant' if mode is not None else 'verified_source_default'
    mode = mode or source_mode(recipe.decode(), cmake.decode())
    if mode is None:
        result['state'] = 'BUILD_MODE_RESOLUTION_REQUIRED'
        return result
    result.update(base_app_mode=mode, base_app_mode_evidence={'mode': mode, 'source_commit': commit, 'origin': origin})
    if unresolved:
        result['state'] = 'DEPENDENCY_RESOLUTION_REQUIRED'
        return result
    conflicts = {name: {'declared': exact[name], 'requested': value}
                 for name, value in overrides.items() if name in exact and not satisfies(exact[name], value)}
    # Only an explicitly selected successful baseline may justify a replay
    # X-Lidar substitution. It must still pass the exact artifact-lock checks
    # below; other source conflicts remain blocked.
    baseline_override = bool(conflicts) and set(conflicts) == {'xlidar'} and request.get('baseline_build') is not None
    if conflicts and not baseline_override:
        result.update(state='DEPENDENCY_OVERRIDE_CONFLICT', override_conflicts=conflicts)
        return result
    constraints = dict(exact)
    constraints.update({name: value for name, value in overrides.items() if name in exact})
    explicit = request.get('baseline_build')
    if explicit is not None and not re.fullmatch(r'[1-9][0-9]*', str(explicit)):
        raise ValueError('Invalid explicit baseline build number')
    tree = 'number,building,result,timestamp,url,actions[parameters[name,value]]'
    if explicit is not None:
        builds = [document(JOB_URL + str(explicit) + '/api/json?' + urlencode({'tree': tree}))]
    else:
        listing = document(JOB_URL + 'api/json?' + urlencode({'tree': 'builds[' + tree + ']{0,40}'}))
        builds = listing.get('builds', [])
    variant = request.get('product_variant')
    if variant is not None and variant not in ('ULTRA', 'MAX'):
        raise ValueError('Unsupported product variant; do not guess a product')
    matches, rejected, checks = [], [], 0
    for build in sorted(builds, key=lambda b: b.get('number', 0), reverse=True):
        number = build['number']
        if build.get('building') is not False or build.get('result') != 'SUCCESS':
            continue
        if build.get('url') != JOB_URL + str(number) + '/':
            raise ValueError('Baseline belongs to another Jenkins job')
        params = parameters(build)
        if params.get('CONTROLLER') != 'CCU_M100_AVMLIVC' or params.get('BUILDVARIANT') != 'release':
            continue
        declared = {}
        for line in (params.get('APP_VERSION', '') + '\n' + params.get('LOCKED_LIBS', '')).splitlines():
            if REF.fullmatch(line.strip()):
                name = line.strip().split('/', 1)[0]
                if name in declared and declared[name] != line.strip().split('#')[0]:
                    raise ValueError('Conflicting baseline parameter dependencies')
                declared[name] = line.strip().split('#')[0]
        if overrides.get('conan_shared') and declared.get('conan_shared') != overrides['conan_shared']:
            continue
        if any(name in declared and not satisfies(value, declared[name]) for name, value in constraints.items()):
            continue
        if checks >= max_lock_checks:
            rejected.append({'code': 'SEARCH_LIMIT_REACHED'})
            break
        checks += 1
        query = ARTIFACT + 'api/search/artifact?' + urlencode({'name': '*-' + str(number) + '.lock', 'repos': 'xcu-acore-test-local'})
        locations = sorted({uri for item in document(query).get('results', [])
                            if (uri := storage_uri(item.get('uri', '')))})
        valid = []
        for uri in locations:
            filename = uri.rsplit('/', 1)[-1]
            product = 'ULTRA' if '_ULTRA_' in filename else 'MAX' if '_MAX_' in filename else None
            if not re.fullmatch(r'CCU_LIVC_AVM-release-[A-Za-z0-9_.-]+-' + str(number) + r'\.lock', filename):
                continue
            if variant and product != variant:
                continue
            metadata = document(uri)
            lock_url = uri.replace('/api/storage/', '/', 1)
            body = raw(lock_url)
            if sha(body) != metadata.get('checksums', {}).get('sha256'):
                raise ValueError('Baseline lock checksum mismatch')
            refs = host_refs(json.loads(body))
            if any(not satisfies(value, refs.get(name, '')) for name, value in constraints.items()):
                continue
            valid.append({'number': number, 'build_url': build['url'], 'product_variant': product,
                          'manifest': filename[:-5], 'lock_url': lock_url, 'lock_sha256': sha(body),
                          'lock_refs': refs, 'parameters': params, 'storage_uri': uri})
        if len(valid) > 1:
            rejected.append({'number': number, 'code': 'AMBIGUOUS_LOCK_ARTIFACTS'})
        else:
            matches.extend(valid)
    result['candidate_baselines'] = matches
    result['discovery_notes'] = rejected
    if not matches:
        result['state'] = 'BASELINE_SEARCH_INCOMPLETE' if rejected else 'NO_VERIFIED_BASELINE'
        return result
    products = {m['product_variant'] for m in matches}
    if len(products) != 1 or None in products or any(x['code'] in ('SEARCH_LIMIT_REACHED', 'AMBIGUOUS_LOCK_ARTIFACTS') for x in rejected):
        result['state'] = 'NEEDS_BASELINE_SELECTION'
        return result
    chosen = matches[0]
    integration = chosen['lock_refs'].get('livc_integrate', '')
    match = REF.fullmatch(integration)
    if not match or not match[5]:
        result['state'] = 'INTEGRATION_PACKAGE_LIST_REQUIRED'
        return result
    package_list = None
    for remote in ('liconan-dev-local', 'liconan-release-local'):
        url = ARTIFACT + remote + '/' + '/'.join((match[3], match[1], match[2], match[4], match[5], 'export/package.lst'))
        try:
            package_list = raw(url, 256 * 1024)
            break
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        try:
            exported = raw(url.rsplit('/', 1)[0] + '/conan_export.tgz')
            with tarfile.open(fileobj=io.BytesIO(exported), mode='r:gz') as archive:
                files = [m for m in archive.getmembers() if m.name in ('package.lst', './package.lst')]
                if len(files) != 1 or not files[0].isfile() or files[0].size > 256 * 1024:
                    raise ValueError('Invalid integration export package list')
                package_list = archive.extractfile(files[0]).read(256 * 1024 + 1)
            break
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    if package_list is None:
        result['state'] = 'INTEGRATION_PACKAGE_LIST_REQUIRED'
        return result
    names = []
    for line in package_list.decode().splitlines():
        name = line.split('#', 1)[0].strip()
        if not name:
            continue
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            raise ValueError('Unsupported integration package list syntax')
        if name not in names:
            names.append(name)
    if 'uroad_m100' not in names:
        result['state'] = 'INCOMPATIBLE_INTEGRATION_PACKAGE_LIST'
        return result
    properties = document(chosen['storage_uri'] + '?properties').get('properties', {})
    inherited = [k for k, values in properties.items() if any(s in k.lower() for s in ('liware', 'limesh')) and any(values)]
    result.update(baseline=chosen, integration_package_names=names,
                  resolved_dependencies={name: chosen['lock_refs'][name].split('#')[0] for name in exact},
                  integration_package_list_sha256=sha(package_list),
                  baseline_artifact_checks={'verified': True, 'liware_present': any('liware' in k.lower() for k in inherited),
                                            'limesh_present': any('limesh' in k.lower() for k in inherited)})
    if baseline_override:
        result['dependency_adjustments'] = {name: dict(item, baseline=chosen['number'],
            lock_sha256=chosen['lock_sha256'], locked_ref=chosen['lock_refs'][name])
            for name, item in conflicts.items()}
    result['state'] = 'IMAGE_BASELINE_REQUIRES_REVIEW' if inherited else 'RESOLVED_FOR_REVIEW'
    result['discovery_sha256'] = fingerprint(result)
    return result


def common_inputs(discovery, release_parameters=None):
    if discovery.get('state') != 'RESOLVED_FOR_REVIEW':
        raise ValueError('Resolve source, baseline and integration list before planning')
    expected = discovery.get('discovery_sha256')
    if fingerprint({k: v for k, v in discovery.items() if k != 'discovery_sha256'}) != expected:
        raise ValueError('Discovery evidence changed')
    baseline = discovery['baseline']
    params = baseline['parameters']
    shared = [line.strip() for line in params['APP_VERSION'].splitlines() if line.strip().startswith('conan_shared/')]
    if len(shared) != 1 or not REF.fullmatch(shared[0]):
        raise ValueError('Baseline does not identify one conan_shared')
    apps, libs = [shared[0]], []
    for name, ref in discovery['resolved_dependencies'].items():
        (apps if name in discovery['integration_package_names'] else libs).append(ref)
    for name, ref in discovery.get('component_overrides', {}).items():
        if name not in discovery['resolved_dependencies'] and name not in ('conan_shared', 'uroad_m100'):
            (apps if name in discovery['integration_package_names'] else libs).append(ref)
    release = dict(release_parameters) if isinstance(release_parameters, dict) else None
    if release is not None: release.setdefault('SOFTWARE_BRANCH', '0111')
    if (not isinstance(release, dict) or set(release) != {'BASE_LINE', 'SOFTWARE_BRANCH', 'BUILDVARIANT'}
            or not re.fullmatch(r'[0-9A-F]{4}', str(release.get('BASE_LINE', '')))
            or release.get('SOFTWARE_BRANCH') != '0111'
            or release.get('BUILDVARIANT') not in ('release', 'debug')
            or any(not isinstance(v, str) for v in release.values())):
        raise ValueError('Explicit software version and build variant are required; AVM software ID is fixed to 0111')
    common = dict(CONTROLLER=params['CONTROLLER'], **release)
    common.update(MANIFEST_NAME=baseline['manifest'], BUILD_REPLAY=True, UPLOAD_ALL_PACKAGE=False,
                  RUN_INTE_TEST=False, SILENT_BUILD=False, NOTIFY_CHAT_ID='', NOTIFY_USER='',
                  LIWARE_URL='', LIMESH_URL='', PORTAL_TRIGGER_URL='', PORTAL_RESOURCE_KEY='')
    return {'source': copy.deepcopy(discovery['source']), 'common_parameters': common,
            'm100_recipe_ref': discovery.get('component_overrides', {}).get('uroad_m100'),
            'base_app_mode': discovery['base_app_mode'],
            'base_app_mode_evidence': copy.deepcopy(discovery['base_app_mode_evidence']),
            'common_app_refs': apps, 'common_locked_refs': libs,
            'baseline_lock_sha256': baseline['lock_sha256'],
            'baseline_artifact_checks': copy.deepcopy(discovery['baseline_artifact_checks']),
            'integration_package_names': list(discovery['integration_package_names']),
            'integration_package_list_sha256': discovery['integration_package_list_sha256'],
            'source_discovery_sha256': expected}


def verify_recipe(reader, ref, revision=None):
    """Read a pinned recipe, or require a unique published revision; never pick latest."""
    match = REF.fullmatch(ref)
    if not match or match[5] or (revision is not None and not re.fullmatch(r'[a-f0-9]{1,64}', revision)):
        raise ValueError('Use an exact component version and verified revision')
    for remote in ('liconan-dev-local', 'liconan-release-local'):
        relative = remote+'/'+'/'.join((match[3], match[1], match[2], match[4]))+'/'
        selected = revision
        try:
            if selected is None:
                listing = json.loads(reader.get(ARTIFACT+'api/storage/'+relative, 1024*1024))
                revisions = [x['uri'].strip('/') for x in listing.get('children', []) if x.get('folder')]
                if len(revisions) != 1 or not re.fullmatch(r'[a-f0-9]{1,64}', revisions[0]):
                    return {'state': 'NEEDS_REVISION_SELECTION', 'ref': ref}
                selected = revisions[0]
            file = relative+selected+'/export/conanfile.py'
            metadata = json.loads(reader.get(ARTIFACT+'api/storage/'+file, 1024*1024))
            raw = reader.get(ARTIFACT+file, 1024*1024)
            if sha(raw) != metadata.get('checksums', {}).get('sha256'):
                raise ValueError('Recipe checksum mismatch')
            return {'state': 'VERIFIED', 'ref': ref, 'recipe_revision': selected,
                    'recipe_sha256': sha(raw), 'repository': remote}
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    return {'state': 'NOT_FOUND', 'ref': ref}


class LiveReader:
    def __init__(self, gateway_root, owner, *, budget_seconds=90):
        if not 1 <= budget_seconds <= 120:
            raise ValueError('Read budget must be between 1 and 120 seconds')
        self.deadline = time.monotonic() + budget_seconds
        self.cache = {}
        root = Path(gateway_root).resolve()
        sys.path.insert(0, str(root))
        from bench_gateway.m100_source_auth import load
        from bench_gateway.m100_ci import authorization
        from bench_gateway.download_auth import load_authorization
        cfg = json.loads((root / 'config/gateway.json').read_text())
        cfg['project_root'] = str(root)
        self.headers = {'gitlab.chehejia.com': {'PRIVATE-TOKEN': load(cfg)},
                        'ci.ep.chehejia.com': {'Authorization': authorization(cfg)},
                        'artifactory.ep.chehejia.com': {'Authorization': load_authorization(cfg, owner, 'artifactory.ep.chehejia.com')}}
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError('Redirect refused; credentials not forwarded')
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, url, limit):
        parsed = urlsplit(url)
        headers = self.headers.get(parsed.hostname)
        if parsed.scheme != 'https' or parsed.port not in (None, 443) or not headers or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('Unapproved read endpoint')
        if not all(headers.values()):
            raise ValueError('Configured read authentication unavailable')
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Read-only configuration validation budget exhausted')
        if url in self.cache:
            body = self.cache[url]
            if len(body) > limit:
                raise ValueError('Read response exceeds bounded limit')
            return body
        request = urllib.request.Request(url, headers=headers, method='GET')
        with self.opener.open(request, timeout=min(10, remaining)) as response:
            chunks, size = [], 0
            while size <= limit:
                if time.monotonic() >= self.deadline:
                    raise TimeoutError('Read-only configuration validation budget exhausted')
                chunk = response.read1(min(65536, limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk);size += len(chunk)
            body = b''.join(chunks)
        if time.monotonic() > self.deadline:
            raise TimeoutError('Read-only configuration validation budget exhausted')
        if len(body) > limit:
            raise ValueError('Read response exceeds bounded limit')
        self.cache[url] = body
        return body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--gateway-root', type=Path, required=True)
    parser.add_argument('--owner', required=True, help='Existing registered requester; never a credential')
    args = parser.parse_args()
    if sys.platform == 'win32' or not args.output.is_absolute() or Path('/mnt/data/bench-eval') not in args.output.resolve().parents:
        raise ValueError('Business evidence must be written under the Linux workstation business root')
    result = discover(json.loads(args.request.read_text()), LiveReader(args.gateway_root, args.owner))
    with args.output.open('x') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'state': result['state'], 'jenkins_submitted': False, 'bench_tasks': 0}))


if __name__ == '__main__':
    main()
