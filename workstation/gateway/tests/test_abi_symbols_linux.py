"""Real readelf boundary on tiny inert AArch64 ELF fixtures; no code execution."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest

from bench_gateway import abi_symbols as abi, material_inspector as inspector


CAMERA='_ZN7XCamera5startEv'
LIDAR='_ZN6XLidar8callbackEv'


def elf(symbols, section_symbols=False):
    names=b'\0.shstrtab\0.dynstr\0.dynsym\0'
    strings=b'\0'; entries=bytes(24)
    if section_symbols:
        # Legal unnamed STT_SECTION / STB_LOCAL entries before global names.
        # No compiler or executable code is involved in these inert fixtures.
        for section in (1,2):
            entries+=struct.pack('<IBBHQQ',0,0x03,0,section,0,0)
    for name,undefined in symbols:
        offset=len(strings);strings+=name.encode()+b'\0'
        entries+=struct.pack('<IBBHQQ',offset,0x12,0,0 if undefined else 1,0,0)
    data=bytearray(64)
    shstr_offset=len(data);data+=names
    dynstr_offset=len(data);data+=strings
    data+=bytes((-len(data))%8)
    dynsym_offset=len(data);data+=entries
    section_offset=len(data)
    data+=bytes(64)
    for name,kind,offset,size,link,info,align,entry_size in (
        # Unnamed section headers ensure GNU readelf cannot substitute a
        # section name for the intentionally unnamed LOCAL SECTION records.
        (0 if section_symbols else 1,3,shstr_offset,len(names),0,0,1,0),
        (0 if section_symbols else 11,3,dynstr_offset,len(strings),0,0,1,0),
        (19,11,dynsym_offset,len(entries),2,3 if section_symbols else 1,8,24)):
        data+=struct.pack('<IIQQQQIIQQ',name,kind,0,0,offset,size,link,info,align,entry_size)
    identity=b'\x7fELF'+bytes((2,1,1))+bytes(9)
    data[:64]=struct.pack('<16sHHIQQQIHHHHHH',identity,3,183,1,0,0,section_offset,0,64,0,0,64,4,1)
    return bytes(data)


@unittest.skipUnless(os.name=='posix' and Path(abi.READELF).is_file(),'requires Linux readelf')
class LinuxReadelfTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.assets={}
        self.write('libxcamera.so',[(CAMERA,False)])
        self.write('libxlidar.so',[(LIDAR,False)])
        self.write('liburoad_m100.so',[(CAMERA,True),(LIDAR,True)])

    def write(self,name,symbols,section_symbols=False):
        path=self.root/name;path.write_bytes(elf(symbols,section_symbols=section_symbols))
        self.assets[name]={'remote_path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

    def test_real_readelf_matches_inert_aarch64_files_without_loading_them(self):
        before={p.name:p.read_bytes() for p in self.root.iterdir()}
        result=abi.check_libraries(self.assets)
        self.assertEqual('LIMITED_PROVIDER_SYMBOLS_MATCH',result['status'])
        self.assertEqual(2,result['required_symbol_count'])
        self.assertEqual('NOT_CHECKED',result['full_abi_compatibility'])
        self.assertFalse(result['so_executed'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir()})

    def test_real_readelf_missing_provider_is_actionable(self):
        self.write('libxlidar.so',[('_ZN6XLidar3oldEv',False)])
        result=abi.check_libraries(self.assets)
        self.assertEqual([{'provider':'libxlidar.so','symbol':LIDAR}],result['missing_symbols'])

    def test_real_readelf_accepts_unnamed_local_section_metadata_without_losing_requirements(self):
        self.write('libxcamera.so',[(CAMERA,False)],section_symbols=True)
        self.write('libxlidar.so',[(LIDAR,False)],section_symbols=True)
        self.write('liburoad_m100.so',[(CAMERA,True),(LIDAR,True)],section_symbols=True)
        with (self.root/'liburoad_m100.so').open('rb') as stream:
            output=abi._readelf(stream,abi.time.monotonic()+5,abi.MAX_OUTPUT_BYTES)
        unnamed_sections=[line.split() for line in output.splitlines()
                          if len(line.split())==7 and line.split()[3:5]==['SECTION','LOCAL']]
        self.assertEqual(2,len(unnamed_sections),output)
        result=abi.check_libraries(self.assets)
        self.assertEqual('LIMITED_PROVIDER_SYMBOLS_MATCH',result['status'])
        self.assertEqual(2,result['required_symbol_count'])
        self.assertFalse(result['so_executed'])
        self.write('libxlidar.so',[('_ZN6XLidar3oldEv',False)],section_symbols=True)
        missing=abi.check_libraries(self.assets)
        self.assertEqual('LIMITED_PROVIDER_SYMBOLS_MISSING',missing['status'])
        self.assertEqual([{'provider':'libxlidar.so','symbol':LIDAR}],missing['missing_symbols'])

    def test_preparation_style_injected_inspector_executes_real_symbol_check(self):
        app=self.root/'fixture.app.tar.gz'
        with tarfile.open(app,'w:gz') as archive:
            for name in abi.LIBRARIES:
                raw=(self.root/name).read_bytes();entry=tarfile.TarInfo('livc/lib/'+name);entry.size=len(raw)
                archive.addfile(entry,io.BytesIO(raw))
        request={'owner_root':str(self.root),'package':{'remote_path':str(app),
                  'sha256':hashlib.sha256(app.read_bytes()).hexdigest()}}
        for role,name in zip(('camera_so','lidar_so','m100_so'),abi.LIBRARIES):
            request[role]={**self.assets[name],'target':'lib/'+name}
        source=base64.b64encode(Path(abi.__file__).read_bytes()).decode()
        bootstrap=("import base64,sys,types\nm=types.ModuleType('bench_gateway_abi_symbols')\n"
                   "exec(compile(base64.b64decode("+repr(source)+"),'abi_symbols.py','exec'),m.__dict__)\n"
                   "sys.modules['bench_gateway_abi_symbols']=m\n")
        completed=subprocess.run([sys.executable,'-',base64.b64encode(json.dumps(request).encode()).decode()],
              input=bootstrap+Path(inspector.__file__).read_text(encoding='utf-8'),text=True,
              encoding='utf-8',capture_output=True,timeout=15,check=True)
        result=json.loads(completed.stdout)
        self.assertEqual('PASS',result['status'],result.get('issues'))
        self.assertEqual('LIMITED_PROVIDER_SYMBOLS_MATCH',result['evidence']['limited_provider_symbols']['status'])


if __name__=='__main__':unittest.main()
