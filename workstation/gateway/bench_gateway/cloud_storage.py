"""Narrow official-SDK transport; credentials never enter requests or output."""
import contextlib
import io
import json
import logging
from pathlib import Path, PurePosixPath
import sys
import time

VOLUME = '2da0a26d1ab44fed845235a9fcd56c98'
PREFIX = 'bench-eval-offline/developer/'
UPLOAD_NAMES = {'request.json','request.sig','predictions.tar.gz','profile.json','public.pem',
                'cloud_evaluation.py','cloud_agent.py','cloud_truth.py','bench_cloud_server.py','report_scope.py','parse_eval_report.py'}


def operate(request):
    prefix = request['remote_prefix']
    if not prefix.startswith(PREFIX) or not prefix.endswith('/') or '..' in PurePosixPath(prefix).parts or '\\' in prefix:
        raise ValueError('only the dedicated output prefix is writable')
    import asset
    from asset.cli.cmd import _init_config
    from asset.io import S3Client
    from asset.util import parse_s3_path
    volume = asset.resource('volumes/'+VOLUME, config=_init_config())
    info = volume.get()
    spec = info['spec']
    spec = json.loads(spec) if isinstance(spec,str) else spec
    location = parse_s3_path(spec['dir']['path'])
    client = S3Client(volume.get_s3_sts)
    key_root = location['path']+prefix

    def read(name, maximum=1024*1024):
        if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
            raise ValueError('unsafe object name')
        body = client.get_object(endpoint=location['endpoint'],bucket=location['bucket'],key=key_root+name)
        try:
            raw = body.read(maximum+1)
            if len(raw)>maximum:
                raise ValueError('oversized evaluation metadata')
            return raw
        finally:
            body.close()

    action = request['action']
    if action=='upload':
        directory=Path(request['local_directory']).resolve(strict=True)
        files=request['files']
        if not files or len(files)!=len(set(files)) or set(files)-UPLOAD_NAMES:
            raise ValueError('unsupported cloud transport file')
        ordered=[n for n in files if n!='request.sig']+(['request.sig'] if 'request.sig' in files else [])
        started=time.monotonic()
        for name in ordered:
            path=directory/name
            if path.is_symlink() or not path.is_file() or path.stat().st_size>2*1024**3:
                raise ValueError('invalid upload file')
            client.upload_file(str(path),endpoint=location['endpoint'],bucket=location['bucket'],
                               key=key_root+name,size=path.stat().st_size)
        listing=client.list_objects(endpoint=location['endpoint'],bucket=location['bucket'],prefix=key_root,delimiter='/',max_keys=100)
        sizes={r['Key'][len(key_root):]:r['Size'] for r in listing.get('Contents',[])}
        if not all(sizes.get(n)==(directory/n).stat().st_size for n in files):
            raise ValueError('cloud upload size differs')
        return {'state':'UPLOADED','bytes':sum((directory/n).stat().st_size for n in files),'seconds':time.monotonic()-started}
    if action=='heartbeat':
        return json.loads(read('worker-status.json',maximum=64*1024))
    if action=='status':
        try:
            return json.loads(read('state.json'))
        except Exception as error:
            return {'state':'NOT_OBSERVED','error_type':type(error).__name__}
    if action=='collect':
        receipt=json.loads(read('attempt/receipt.json'))
        cloud=PurePosixPath(request['cloud_directory'])
        report=PurePosixPath(receipt['report_path'])
        relative=report.relative_to(cloud).as_posix()
        if not relative.startswith('attempt/eval_report/') or report.suffix!='.txt':
            raise ValueError('report outside stable attempt')
        raw=read(relative,16*1024**2)
        import base64
        return {'receipt':receipt,'report_base64':base64.b64encode(raw).decode()}
    raise ValueError('unsupported cloud transport action')


def main():
    logging.disable(logging.CRITICAL)
    request=json.loads(sys.stdin.read(1024*1024))
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        try:
            result={'ok':True,'data':operate(request)}
        except Exception as error:
            result={'ok':False,'error_type':type(error).__name__}
    print(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':
    main()
