"""Derive an exact reviewed APP copy without extraction or bench operations."""
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tarfile
import time


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def derive(source,destination,assembly,deadline):
    """Only three reviewed changes are legal; every other tar member is copied."""
    changed=[];seen=set();unchanged=0
    dep=assembly['dependency'];dependency=Path(dep['source'])
    assert sha(dependency)==dep['sha256'],'reviewed dependency changed'
    patches=assembly['yaml_patches']
    with tarfile.open(source,'r|gz') as original,Path(destination).open('xb') as raw:
        with gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0,compresslevel=1) as compressed:
            with tarfile.open(fileobj=compressed,mode='w|',format=tarfile.PAX_FORMAT) as result:
                for member in original:
                    assert time.monotonic()<deadline,'assembly time budget reached'
                    name=member.name[2:] if member.name.startswith('./') else member.name
                    assert not PurePosixPath(name).is_absolute() and '..' not in PurePosixPath(name).parts
                    target=name[len(assembly['archive_prefix']):] if name.startswith(assembly['archive_prefix']) else name
                    assert name not in seen or member.isdir(),'duplicate archive member'
                    seen.add(name)
                    assert target!=dep['target'],'dependency already present in source APP'
                    if target in patches:
                        patch=patches[target];assert member.isfile() and member.size<=2*1024**2
                        before=original.extractfile(member).read()
                        assert hashlib.sha256(before).hexdigest()==patch['before_sha256'],'configuration source changed'
                        text=before.decode('utf-8');assert text.count(patch['old'])==1
                        after=text.replace(patch['old'],patch['new'],1).encode('utf-8')
                        assert hashlib.sha256(after).hexdigest()==patch['after_sha256'],'configuration derivation differs'
                        entry=copy.copy(member);entry.size=len(after);entry.pax_headers=dict(member.pax_headers)
                        entry.pax_headers.pop('size',None)
                        result.addfile(entry,io.BytesIO(after));changed.append(target)
                    else:
                        result.addfile(member,original.extractfile(member) if member.isfile() else None);unchanged+=1
                assert set(changed)==set(patches),'reviewed YAML target missing'
                entry=tarfile.TarInfo(assembly['archive_prefix']+dep['target']);entry.size=dependency.stat().st_size
                entry.mode=0o644;entry.mtime=0;entry.uid=0;entry.gid=0;entry.uname='root';entry.gname='root'
                with dependency.open('rb') as content:result.addfile(entry,content)
                changed.append(dep['target'])
    return {'changed_members':changed,'unchanged_members':unchanged,'sha256':sha(destination),'size':Path(destination).stat().st_size}


def run(request):
    import fcntl
    root=Path(request['owner_root']);source=Path(request['package']['remote_path']);assembly=request['assembly']
    assert root.is_absolute() and not any(p.is_symlink() for p in (root,*root.parents))
    assert source.resolve(strict=True)==source and root in source.parents and source.is_file()
    assert request['key']==hashlib.sha256(json.dumps(assembly,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()[:32]
    directory=root/'prepared-packages'/request['key']
    directory.mkdir(parents=True,exist_ok=True)
    assert directory.resolve()==directory and root in directory.parents
    deadline=time.monotonic()+120
    with (directory/'assembly.lock').open('a+b') as lock:
        while True:
            try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                assert time.monotonic()<deadline,'assembly already active';time.sleep(.2)
        receipt=directory/'receipt.json';output=directory/'package.app.tar.gz'
        if receipt.exists():
            value=json.loads(receipt.read_text())
            assert value['state']=='PREPARED' and value['source_sha256']==assembly['source_sha256']
            assert output.is_file() and not output.is_symlink() and sha(output)==value['sha256']
            assert value['sha256']==assembly['output_sha256'],'prepared package identity changed'
            return {**value,'reused':True}
        assert not (directory/'attempt.json').exists(),'previous assembly incomplete; inspect before retry'
        assert sha(source)==assembly['source_sha256']==request['package']['sha256'],'original APP changed'
        info=source.stat();before=(info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
        (directory/'attempt.json').write_text(json.dumps({'started_at':time.time(),'recipe':request['recipe']}))
        temp=directory/'package.partial'
        value=derive(source,temp,assembly,deadline)
        info=source.stat();assert before==(info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns),'original APP changed during assembly'
        assert value['sha256']==assembly['output_sha256'],'assembled package differs from reviewed identity'
        os.chmod(temp,0o600);os.replace(temp,output)
        value.update(state='PREPARED',source_sha256=assembly['source_sha256'],remote_path=str(output),recipe=request['recipe'],
                     hardware_actions='NOT_RUN',original_modified=False,reused=False)
        pending=directory/'receipt.tmp';pending.write_text(json.dumps(value,sort_keys=True));os.replace(pending,receipt)
        return value


if __name__=='__main__':
    try:print(json.dumps(run(json.load(sys.stdin))))
    except Exception as exc:print(json.dumps({'state':'NEEDS_REVIEW','error_type':type(exc).__name__}))
