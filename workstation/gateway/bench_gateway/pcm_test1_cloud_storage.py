"""Explicit-account signed inbox transport for the dedicated PCM test-1 receiver."""
import argparse
import base64
import contextlib
import hashlib
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import time
import uuid

KIND = 'pcm-test1-cloud-storage-v1'
INSTANCE = 'benchflow-pcm-test1'
ACCOUNT = 'developer'
VOLUME_NAME = 'vc-chassis-bd-ga'
PREFIX = 'benchflow-pcm-test1/developer/runs/'
RUN_ROOT = '/mnt/volumes/vc-chassis-bd-ga/' + PREFIX.rstrip('/')
PROBE_PREFIX = 'benchflow-pcm-test1/developer/deployment-probes/'
INCOMING_PREFIX = 'benchflow-pcm-test1/developer/incoming/'
INGRESS = 'pcm-test1-ingress-v1'
TASK = re.compile(r'u_[0-9a-f]{24}/cloud-eval-[0-9]{8}-[0-9a-f]{12}/\Z')
FILES = {'predictions.tar.gz': 2 * 1024**3, 'request.json': 256 * 1024, 'request.sig': 64}
IDENTITY_FIELDS = {'originEmail', 'userId', 'userEmail', 'email', 'mail', 'username', 'userName', 'name'}


class ObjectAbsent(Exception):
    pass


class SubmissionPending(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def object_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate object member')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError('object required')
    return value


def no_links(path, missing_ok=False):
    path = Path(path)
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if missing_ok:
                continue
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('links and reparse points are not accepted')


def regular_bytes(path, maximum):
    path = Path(path)
    no_links(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or getattr(before, 'st_file_attributes', 0) & 0x400:
        raise ValueError('regular file required')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(path, flags), 'rb') as source:
        opened = os.fstat(source.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_size > maximum
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError('bounded regular file required')
        raw = source.read(maximum + 1)
        after = os.fstat(source.fileno())
    if len(raw) > maximum or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('file changed while reading')
    return raw


def validate_binding(binding):
    if set(binding) != {'schema_version', 'kind', 'credential_file', 'profile_identity', 'volume_name', 'profile',
                        'sender_state_root', 'node_binary', 'signer_script', 'signing_private_key'}:
        raise ValueError('invalid storage binding fields')
    if binding['schema_version'] != 1 or binding['kind'] != KIND or binding['volume_name'] != VOLUME_NAME:
        raise ValueError('invalid storage binding')
    if not isinstance(binding['credential_file'], str) or not Path(binding['credential_file']).is_absolute():
        raise ValueError('explicit credential path required')
    for field in ('sender_state_root', 'node_binary', 'signer_script', 'signing_private_key'):
        if not isinstance(binding[field], str) or not Path(binding[field]).is_absolute():
            raise ValueError('explicit sender control path required')
    if Path(binding['signer_script']).resolve() != Path(__file__).resolve().parents[1] / 'tools/cloud_sign.js':
        raise ValueError('fixed reviewed signer required')
    identity = binding['profile_identity']
    if (not isinstance(identity, dict) or set(identity) != {'field', 'value'}
            or identity['field'] not in IDENTITY_FIELDS or not isinstance(identity['value'], str)
            or not 1 <= len(identity['value']) <= 200 or any(ord(c) < 32 for c in identity['value'])):
        raise ValueError('verified profile identity required')
    profile = binding['profile']
    expected = {'instance': INSTANCE, 'namespace': 'vc-chassis_u-developer', 'workspace': 'vc-chassis',
                'cluster': 'cnhb01-algorithm-bidu-p-03', 'kind': 'lpai-mounted-v1',
                'run_root': RUN_ROOT, 'volume_prefix': PREFIX, 'gt_policy': 'task-dataset-v1',
                'ingress_policy': 'signed-envelope-v1',
                'ingress_root': '/mnt/volumes/vc-chassis-bd-ga/' + INCOMING_PREFIX.rstrip('/'),
                'ingress_claim_root': '/lpai/benchflow-pcm-test1/claims'}
    if not isinstance(profile, dict) or any(profile.get(k) != v for k, v in expected.items()):
        raise ValueError('test-1 profile identity differs')
    if not isinstance(profile.get('volume_uid'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,100}', profile['volume_uid']):
        raise ValueError('verified volume uid required')
    body = {key: value for key, value in profile.items() if key != 'profile_sha256'}
    if hashlib.sha256(canonical(body)).hexdigest() != profile.get('profile_sha256'):
        raise ValueError('profile hash differs')
    return binding


def load_binding(path):
    if not Path(path).is_absolute():
        raise ValueError('absolute storage binding path required')
    return validate_binding(object_json(regular_bytes(path, 1024 * 1024)))


class SDKStore:
    """Only official SDK identity/STS and bounded boto3 object operations."""
    def __init__(self, binding):
        import asset
        from asset.util import parse_s3_path
        credential = object_json(regular_bytes(binding['credential_file'], 64 * 1024))
        if (set(credential) != {'schema_version', 'username', 'jwt_token'}
                or credential['schema_version'] != 1 or credential['username'] != ACCOUNT
                or not isinstance(credential['jwt_token'], str) or not credential['jwt_token'].strip()):
            raise ValueError('explicit test-1 credential required')
        cfg = asset.config(env='prod', jwt_token=credential.pop('jwt_token'), scopes='default', grey=False)
        identity = asset.client(cfg).profile()
        expected = binding['profile_identity']
        if identity.get(expected['field']) != expected['value']:
            raise ValueError('SDK account differs from verified account')
        self.volume = asset.resource('volumes/' + binding['profile']['volume_uid'], config=cfg)
        info = self.volume.get()
        metadata = info.get('metadata', {})
        if metadata.get('name') != VOLUME_NAME or metadata.get('uid') != binding['profile']['volume_uid']:
            raise ValueError('SDK output volume identity differs')
        spec = info['spec']
        spec = object_json(spec) if isinstance(spec, str) else spec
        self.location = parse_s3_path(spec['dir']['path'])
        root = self.location['path']
        if not isinstance(root, str) or '\\' in root or '..' in PurePosixPath(root).parts:
            raise ValueError('invalid volume storage root')
        self.key_root = root.rstrip('/') + '/' if root else ''
        self.clients = {}

    def client(self, use):
        if use not in self.clients:
            import boto3
            from botocore.config import Config
            from asset.util import parse_s3_path
            mappings = self.volume.get_s3_sts(use=use, storage='')
            matching = []
            for source, auth in mappings.items():
                location = parse_s3_path(source)
                if (location['endpoint'].rstrip('/') == self.location['endpoint'].rstrip('/')
                        and location['bucket'] == self.location['bucket']
                        and location['path'].rstrip('/') == self.location['path'].rstrip('/')
                        and auth.get('endpoint_url', '').rstrip('/') == self.location['endpoint'].rstrip('/')):
                    matching.append(auth)
            if len(matching) != 1:
                raise ValueError('ambiguous volume authorization')
            auth = matching[0]
            self.clients[use] = boto3.client('s3', region_name=auth['region_name'],
                endpoint_url=auth['endpoint_url'], aws_access_key_id=auth['aws_access_key_id'],
                aws_secret_access_key=auth['aws_secret_access_key'], aws_session_token=auth['aws_session_token'],
                config=Config(connect_timeout=10, read_timeout=120,
                              retries={'total_max_attempts': 1, 'mode': 'standard'},
                              s3={'addressing_style': auth['addressing_style'], 'signature_version': 's3v4'}))
        return self.clients[use]

    def read(self, key, maximum):
        try:
            reply = self.client('download').get_object(Bucket=self.location['bucket'], Key=self.key_root + key)
        except Exception as error:
            if getattr(error, 'response', {}).get('Error', {}).get('Code') in ('NoSuchKey', 'NotFound', '404'):
                raise ObjectAbsent() from None
            raise
        body = reply['Body']
        try:
            if reply.get('ContentLength', maximum + 1) > maximum:
                raise ValueError('oversized object')
            raw = body.read(maximum + 1)
            if len(raw) > maximum:
                raise ValueError('oversized object')
            return raw
        finally:
            body.close()

    def size(self, key):
        reply = self.client('download').head_object(Bucket=self.location['bucket'], Key=self.key_root + key)
        return reply['ContentLength']

    def write_stage(self, key, path):
        if not re.fullmatch(re.escape(INCOMING_PREFIX) + r'[0-9a-f]{32}/(?:predictions\.tar\.gz|request\.json|request\.sig|probe\.json|envelope\.json|envelope\.sig)', key):
            raise ValueError('writes are restricted to the signed incoming staging area')
        client = self.client('upload')
        with Path(path).open('rb') as source:
            return client.put_object(Bucket=self.location['bucket'], Key=self.key_root + key,
                                     Body=source, ContentLength=Path(path).stat().st_size)


def validate_request(request):
    action = request.get('action')
    fields = {'heartbeat': {'action', 'remote_prefix'}, 'status': {'action', 'remote_prefix'},
              'collect': {'action', 'remote_prefix', 'cloud_directory'},
              'upload': {'action', 'remote_prefix', 'local_directory', 'files'},
              'probe': {'action', 'probe_id'}, 'probe-read': {'action', 'probe_id'}}
    if action not in fields or set(request) != fields[action]:
        raise ValueError('unsupported transport request')
    if action in ('probe', 'probe-read'):
        if not isinstance(request['probe_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', request['probe_id']):
            raise ValueError('invalid deployment probe id')
        return request['probe_id']
    prefix = request['remote_prefix']
    if not isinstance(prefix, str):
        raise ValueError('invalid output prefix')
    if action == 'heartbeat':
        if prefix != PREFIX:
            raise ValueError('heartbeat requires the fixed root')
    elif not prefix.startswith(PREFIX) or not TASK.fullmatch(prefix[len(PREFIX):]):
        raise ValueError('fixed owner and attempt prefix required')
    if action == 'collect' and request['cloud_directory'] != RUN_ROOT + '/' + prefix[len(PREFIX):].rstrip('/'):
        raise ValueError('cloud directory differs from fixed attempt')
    if action == 'upload' and (not isinstance(request['files'], list) or len(request['files']) != 3
                               or set(request['files']) != set(FILES)):
        raise ValueError('only the complete business payload is supported')
    return prefix


def freeze_upload(directory, staging, binding, prefix):
    directory = Path(directory)
    if not directory.is_absolute():
        raise ValueError('absolute local directory required')
    no_links(directory)
    request_raw = regular_bytes(directory / 'request.json', FILES['request.json'])
    request = object_json(request_raw)
    owner = request.get('owner')
    suffix = prefix[len(PREFIX):].rstrip('/').split('/')
    if (not isinstance(owner, str) or not re.fullmatch(r'ou_[A-Za-z0-9_-]{3,100}', owner)
            or request.get('owner_key') != 'u_' + hashlib.sha256(owner.encode()).hexdigest()[:24]
            or request.get('owner_key') != suffix[0] or request.get('run_id') != suffix[1]
            or request.get('profile_sha256') != binding['profile']['profile_sha256']
            or request.get('tool_hashes') != binding['profile'].get('tool_hashes')):
        raise ValueError('signed request identity differs from storage binding')
    signature = regular_bytes(directory / 'request.sig', 64)
    if len(signature) != 64:
        raise ValueError('Ed25519 signature required')
    for name, raw in (('request.json', request_raw), ('request.sig', signature)):
        with (staging / name).open('xb') as output:
            output.write(raw)
    prediction = directory / 'predictions.tar.gz'
    no_links(prediction)
    before = prediction.lstat()
    if not stat.S_ISREG(before.st_mode) or getattr(before, 'st_file_attributes', 0) & 0x400:
        raise ValueError('regular prediction file required')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(prediction, flags), 'rb') as source, (staging / prediction.name).open('xb') as output:
        opened = os.fstat(source.fileno())
        if (not stat.S_ISREG(opened.st_mode) or not 0 < opened.st_size <= FILES[prediction.name]
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError('bounded prediction archive required')
        total = 0
        digest = hashlib.sha256()
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > FILES[prediction.name]:
                raise ValueError('prediction archive grew beyond limit')
            output.write(block)
            digest.update(block)
        after = os.fstat(source.fileno())
    if total != opened.st_size or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('prediction archive changed during snapshot')
    return {'request.json': {'size': len(request_raw), 'sha256': hashlib.sha256(request_raw).hexdigest()},
            'request.sig': {'size': 64, 'sha256': hashlib.sha256(signature).hexdigest()},
            'predictions.tar.gz': {'size': total, 'sha256': digest.hexdigest()}}


def save_intent(path, value, first=False):
    raw = canonical(value) + b'\n'
    if first:
        with path.open('xb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        return
    temporary = path.with_name('intent-' + uuid.uuid4().hex + '.tmp')
    with temporary.open('xb') as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def sign_envelope(binding, folder, raw):
    for field in ('node_binary', 'signer_script', 'signing_private_key'):
        no_links(binding[field])
    signing = folder / 'envelope-signing'
    signing.mkdir(mode=0o700)
    (signing / 'request.json').write_bytes(raw)
    result = subprocess.run([binding['node_binary'], binding['signer_script'], 'sign', binding['signing_private_key'],
                             str(signing / 'request.json'), str(signing / 'request.sig')],
                            capture_output=True, stdin=subprocess.DEVNULL, timeout=20)
    if result.returncode:
        raise ValueError('envelope signing failed')
    signature = regular_bytes(signing / 'request.sig', 64)
    if len(signature) != 64:
        raise ValueError('envelope signature size differs')
    return signature


def accepted_result(store, intent, wait_seconds):
    until = time.monotonic() + wait_seconds
    while True:
        try:
            result = object_json(store.read(INCOMING_PREFIX + intent['submission_id'] + '/ingress-result.json', 64 * 1024))
        except ObjectAbsent:
            if time.monotonic() >= until:
                raise SubmissionPending('same submission requires read-only reconciliation') from None
            time.sleep(min(2, max(0, until - time.monotonic())))
            continue
        expected = {'schema_version': 1, 'kind': 'pcm-test1-ingress-result-v1',
                    'submission_id': intent['submission_id'], 'profile_sha256': intent['profile_sha256'],
                    'purpose': intent['purpose'], 'envelope_sha256': intent['envelope_sha256']}
        if any(result.get(k) != v for k, v in expected.items()):
            raise ValueError('receiver acknowledgement identity differs')
        if result.get('state') != ('PROBE_ACK' if intent['purpose'] == 'probe' else 'ACCEPTED'):
            raise ValueError('receiver did not accept this submission')
        if intent['purpose'] == 'business' and result.get('run_id') != intent['run_id']:
            raise ValueError('receiver acknowledgement task differs')
        return {**result, 'state': 'PROBE_ACK' if intent['purpose'] == 'probe' else 'UPLOADED'}


def submit_or_reconcile(binding, request, store, logical):
    purpose = 'probe' if request['action'] in ('probe', 'probe-read') else 'business'
    state_root = Path(binding['sender_state_root'])
    no_links(state_root, missing_ok=True)
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = 'probe-' + logical if purpose == 'probe' else 'business-' + hashlib.sha256(logical.encode()).hexdigest()
    folder = state_root / name
    no_links(folder, missing_ok=True)
    identity = {'purpose': purpose, 'logical': logical, 'profile_sha256': binding['profile']['profile_sha256']}
    if folder.exists():
        intent = object_json(regular_bytes(folder / 'intent.json', 64 * 1024))
        if any(intent.get(k) != v for k, v in identity.items()) or intent.get('phase') != 'SUBMITTING':
            raise ValueError('existing sender intent requires inspection')
        return accepted_result(store, intent, 0)
    if request['action'] == 'probe-read':
        raise ValueError('no saved probe intent; read-only reconciliation cannot submit')
    folder.mkdir(mode=0o700)
    submission = logical if purpose == 'probe' else uuid.uuid4().hex
    intent = {**identity, 'submission_id': submission, 'phase': 'PREPARING'}
    save_intent(folder / 'intent.json', intent, first=True)
    payload = folder / 'payload'
    payload.mkdir(mode=0o700)
    envelope = {'schema_version': 1, 'kind': INGRESS, 'purpose': purpose, 'submission_id': submission,
                'profile_sha256': identity['profile_sha256']}
    if purpose == 'business':
        envelope['files'] = freeze_upload(request['local_directory'], payload, binding, logical)
        native = object_json((payload / 'request.json').read_bytes())
        envelope['target'] = {key: native[key] for key in ('owner', 'owner_key', 'run_id')}
        intent['run_id'] = native['run_id']
    else:
        raw = canonical({'schema_version': 1, 'kind': 'pcm-test1-transport-probe-v1',
                         'submission_id': submission, 'profile_sha256': identity['profile_sha256']}) + b'\n'
        (payload / 'probe.json').write_bytes(raw)
        envelope['files'] = {'probe.json': {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}}
    raw = canonical(envelope) + b'\n'
    (payload / 'envelope.json').write_bytes(raw)
    (payload / 'envelope.sig').write_bytes(sign_envelope(binding, folder, raw))
    intent.update(phase='SUBMITTING', envelope_sha256=hashlib.sha256(raw).hexdigest())
    save_intent(folder / 'intent.json', intent)
    prefix = INCOMING_PREFIX + submission + '/'
    for name in [*sorted(envelope['files']), 'envelope.json', 'envelope.sig']:
        store.write_stage(prefix + name, payload / name)
    return accepted_result(store, intent, 45)


def operate(binding, request, store=None):
    validate_binding(binding)
    prefix = validate_request(request)
    action = request['action']
    store = store if store is not None else SDKStore(binding)
    if action in ('probe', 'probe-read', 'upload'):
        return submit_or_reconcile(binding, request, store, prefix)
    if action == 'heartbeat':
        result = object_json(store.read(prefix + 'worker-status.json', 64 * 1024))
        if result.get('instance') != INSTANCE or result.get('profile_sha256') != binding['profile']['profile_sha256']:
            raise ValueError('receiver identity differs')
        return result
    if action == 'status':
        try:
            return object_json(store.read(prefix + 'state.json', 1024 * 1024))
        except ObjectAbsent:
            return {'state': 'NOT_OBSERVED'}
    receipt = object_json(store.read(prefix + 'attempt/receipt.json', 1024 * 1024))
    report = PurePosixPath(receipt['report_path'])
    cloud = PurePosixPath(request['cloud_directory'])
    relative = report.relative_to(cloud)
    if (len(relative.parts) != 3 or relative.parts[:2] != ('attempt', 'eval_report')
            or report.suffix != '.txt' or '\\' in str(report) or '..' in relative.parts):
        raise ValueError('report outside the fixed attempt')
    raw = store.read(prefix + relative.as_posix(), 16 * 1024**2)
    if not raw or hashlib.sha256(raw).hexdigest() != receipt.get('report_sha256'):
        raise ValueError('report bytes differ from receipt')
    return {'receipt': receipt, 'report_base64': base64.b64encode(raw).decode()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binding', required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            raw = sys.stdin.buffer.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('oversized transport request')
            result = {'ok': True, 'data': operate(load_binding(args.binding), object_json(raw))}
        except Exception as error:
            result = {'ok': False, 'error_type': type(error).__name__}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
