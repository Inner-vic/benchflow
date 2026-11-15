import json
import os
import shlex
import subprocess
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .config import ssh_binary
from .core import GatewayError, digest, remote_within, within


def download(cfg, store, user, url):
    allowed = set(cfg.get("download_hosts", []))

    def check(value):
        p = urllib.parse.urlsplit(value)
        if p.scheme != "https" or not p.hostname or p.hostname not in allowed or p.username or p.password:
            raise GatewayError("下载地址必须是管理员已允许域名的 HTTPS 链接；需要登录的链接请先在本机下载后入库")
    check(url)

    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            check(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    directory = store.user_root(user) / "inbox"
    directory.mkdir(parents=True, exist_ok=True)
    filename = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
    if not filename or len(filename) > 200 or any(c in filename for c in '\\/:*?"<>|\x00'):
        raise GatewayError("下载文件名无效")
    final = within(store.user_root(user), directory / (uuid.uuid4().hex[:8] + "-" + filename))
    tmp = final.with_suffix(final.suffix + ".partial")
    try:
        with urllib.request.build_opener(Redirect()).open(url, timeout=30) as source, tmp.open("xb") as target:
            length = int(source.headers.get("Content-Length", "0"))
            maximum = cfg.get("max_download_bytes", 8 * 1024**3)
            if length > maximum:
                raise GatewayError("文件超过下载上限")
            total = 0
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                total += len(chunk)
                if total > maximum:
                    raise GatewayError("文件超过下载上限")
                target.write(chunk)
        if not total or (length and total != length):
            raise GatewayError("下载为空或长度不完整")
        os.replace(tmp, final)
        return final
    except GatewayError:
        raise
    except Exception as exc:
        # Exception messages can contain signed URLs; do not echo them.
        raise GatewayError("下载失败或需要身份认证，请在本机下载后入库") from None
    finally:
        tmp.unlink(missing_ok=True)


class SSH:
    def __init__(self, cfg):
        self.cfg = cfg
        self.host = cfg.get("workstation", "ubuntu-workstation")
        if not self.host.replace("-", "").replace("_", "").isalnum():
            raise GatewayError("工作站必须使用固定 SSH 别名")

    def argv(self):
        if self.cfg.get('workstation_local_execution') is True:
            import sys
            return [sys.executable,'-B',str(Path(self.cfg['project_root'])/'bench_gateway/workstation_runtime.py')]
        return [self.cfg.get("ssh_binary") or ssh_binary(), "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", self.host]

    def python(self, code, args=(), timeout=60, fresh=False):
        cmd = "python3 - " + " ".join(shlex.quote(str(v)) for v in args)
        argv=self.argv()
        if fresh and not self.cfg.get('workstation_local_execution'):argv=argv[:-1]+["-o","ControlMaster=no","-o","ControlPath=none",argv[-1]]
        completed = subprocess.run(argv + [cmd], input=code, text=True, encoding="utf-8", capture_output=True, timeout=timeout)
        if completed.returncode:
            raise GatewayError("工作站命令失败：" + (completed.stderr[-1000:] or completed.stdout[-1000:]))
        return completed.stdout

    def inspect_dataset(self, root, manifest):
        code = '''import sys,json,hashlib,pathlib
root=pathlib.Path(sys.argv[1]).resolve(strict=True); p=pathlib.Path(sys.argv[2]).resolve(strict=True)
assert root in p.parents and p.is_file(), 'manifest outside dataset'
v=json.loads(p.read_text(encoding='utf-8')); scenes=v.get('prepared_scenes'); assert isinstance(scenes,list) and scenes
for item in scenes:
    assert item.get('scene')
    data=item.get('archive_stats',item)
    for key in ('scene_root','raw_root','truth_root'):
        if data.get(key):
            q=pathlib.Path(data[key]).resolve(strict=True)
            assert q==root or root in q.parents, 'scene source outside dataset'
print(json.dumps({'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'scene_count':len(scenes)}))
'''
        return json.loads(self.python(code, (root, manifest)))

    def upload(self, store, asset, trusted_source=None):
        user = asset["owner"]
        data = asset["data"]
        source = Path(trusted_source).resolve(strict=True) if trusted_source else within(store.user_root(user), data["local_path"])
        if digest(source) != data["sha256"]:
            raise GatewayError("本地资产内容变化，不能执行")
        remote_root = store.remote_root(user, self.cfg["remote_users_root"])
        remote = remote_within(remote_root, remote_root + "/" + asset["kind"] + "/" + data["sha256"] + "/asset.bin")
        probe='''import pathlib,sys,hashlib,json
root=pathlib.Path(sys.argv[1]);p=pathlib.Path(sys.argv[2]);expected=sys.argv[3]
assert not root.is_symlink()
if p.exists():
    assert root.resolve() in p.resolve().parents and p.is_file() and not p.is_symlink()
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    assert h.hexdigest()==expected,'immutable asset differs'
    print('REUSED')
else:print('UPLOAD')
'''
        if self.python(probe,(remote_root,remote,data['sha256'])).strip()=='REUSED':return remote
        # The byte stream uses SSH directly, never the bot transport. Commit only on matching hash.
        receiver = '''import sys,pathlib,hashlib,os,uuid
root=pathlib.Path(sys.argv[1]); final=pathlib.Path(sys.argv[2]); expected=sys.argv[3]
final.parent.mkdir(parents=True,exist_ok=True)
r=root.resolve(strict=True); p=final.resolve(); assert r in p.parents
temp=final.with_name('.partial-'+uuid.uuid4().hex); h=hashlib.sha256()
try:
    with temp.open('xb') as f:
        while True:
            b=sys.stdin.buffer.read(1048576)
            if not b: break
            h.update(b); f.write(b)
    assert h.hexdigest()==expected, 'transfer digest mismatch'
    os.replace(temp,final)
finally:
    if temp.exists():temp.unlink()
'''
        import base64
        encoded = base64.b64encode(receiver.encode()).decode()
        cmd = "python3 -c " + shlex.quote("import base64;exec(base64.b64decode('" + encoded + "'))") + " " + " ".join(shlex.quote(x) for x in (remote_root, remote, data["sha256"]))
        with source.open("rb") as f:
            result = subprocess.run(self.argv() + [cmd], stdin=f, capture_output=True, timeout=3600)
        if result.returncode:
            raise GatewayError("SSH 文件上传或校验失败")
        return remote
