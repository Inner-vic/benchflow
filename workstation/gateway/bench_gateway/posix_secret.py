"""Workstation-only Fernet envelope helper, using its existing crypto runtime."""
import base64,json,os,stat,sys
from pathlib import Path
from cryptography.fernet import Fernet

def main():
    value=json.load(sys.stdin);path=Path(value['key'])
    if not path.is_absolute() or '..' in path.parts:raise ValueError('invalid key path')
    for p in [path,*path.parents]:
        if p.is_symlink():raise ValueError('key path contains link')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not path.exists():
        try:
            descriptor=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'wb') as out:out.write(Fernet.generate_key())
        except FileExistsError:pass
    info=path.stat()
    if stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1:raise ValueError('key permissions changed')
    f=Fernet(path.read_bytes());raw=base64.b64decode(value['data']);scope=base64.b64decode(value['scope'])+b'\0'
    if value['decrypt']:
        raw=f.decrypt(raw)
        if not raw.startswith(scope):raise ValueError('credential scope mismatch')
        raw=raw[len(scope):]
    else:raw=f.encrypt(scope+raw)
    print(base64.b64encode(raw).decode())

if __name__=='__main__':main()
