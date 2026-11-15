import hashlib,importlib.util,io,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
BASE=Path(__file__).resolve().parent
def module(name,file):
    source=BASE/file
    if not source.exists():source=BASE.parent/'bench_gateway'/file
    spec=importlib.util.spec_from_file_location('bench_gateway.'+name,source)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
files=module('file_fix','feishu_files.py')
assembly=module('assembly_fix','package_assembly.py')
class Response(io.BytesIO):
    def __init__(self,data,status=206,headers=None):super().__init__(data);self.status=status;self.headers=headers or {}
class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'m100.so';self.client=files.FeishuFiles({});self.client.auth=lambda:'test-only'
    def run_download(self,opener,maximum=128*1024*1024):
        with patch.object(files,'urlopen',side_effect=opener),patch.object(files.time,'sleep'):
            return self.client.download('om_test','file_test',self.path,maximum)
    def endpoint(self,data):
        def get(req,timeout):
            start,end=map(int,req.get_header('Range')[6:].split('-'));end=min(end,len(data)-1)
            return Response(data[start:end+1],206,{'Content-Range':f'bytes {start}-{end}/{len(data)}','ETag':'same'})
        return get
    def test_multislice_exact_hash_and_size(self):
        data=b'a'*(8*1024*1024)+b'final';r=self.run_download(self.endpoint(data))
        self.assertEqual(2,r['requests']);self.assertEqual(hashlib.sha256(data).hexdigest(),r['sha256']);self.assertEqual(data,self.path.read_bytes())
    def test_plain_small_response_supported(self):
        r=self.run_download(lambda *a,**k:Response(b'small',200,{'Content-Length':'5'}));self.assertEqual(5,r['bytes'])
    def test_plain_unknown_length_supported(self):
        r=self.run_download(lambda *a,**k:Response(b'small',200));self.assertEqual(5,r['bytes'])
    def test_short_slice_retried_without_duplicate_bytes(self):
        good=self.endpoint(b'abcd');calls=[]
        def get(*a,**k):
            calls.append(1)
            return Response(b'ab',206,{'Content-Range':'bytes 0-3/4'}) if len(calls)==1 else good(*a,**k)
        r=self.run_download(get);self.assertEqual(b'abcd',self.path.read_bytes());self.assertEqual(2,r['requests'])
    def reject(self,response,maximum=128*1024*1024):
        with self.assertRaises(files.GatewayError):self.run_download(lambda *a,**k:response(),maximum)
        self.assertFalse(self.path.exists());self.assertFalse(self.path.with_suffix('.partial').exists())
    def test_reject_missing_range(self):self.reject(lambda:Response(b'ab',206))
    def test_reject_wrong_offset(self):self.reject(lambda:Response(b'ab',206,{'Content-Range':'bytes 1-2/4'}))
    def test_reject_over_limit(self):self.reject(lambda:Response(b'ab',206,{'Content-Range':'bytes 0-1/100'}),maximum=10)
    def test_reject_truncated_repeatedly(self):self.reject(lambda:Response(b'a',206,{'Content-Range':'bytes 0-3/4'}))
    def test_reject_body_longer_than_range(self):self.reject(lambda:Response(b'abc',206,{'Content-Range':'bytes 0-1/2'}))
    def test_reject_json_api_failure(self):
        from urllib.error import HTTPError
        def get(*a,**k):raise HTTPError('https://example.invalid',400,'',{},io.BytesIO(b'{"code":234037}'))
        with self.assertRaisesRegex(files.GatewayError,'234037'):self.run_download(get)
        self.assertFalse(self.path.exists())
class AssemblyMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.project=Path(self.temp.name);(self.project/'bench_gateway').mkdir()
        (self.project/'bench_gateway/package_assembly_remote.py').write_text('pass')
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.execute('create table storage_mappings(owner text,old_prefix text,new_prefix text)')
        self.db.execute('insert into storage_mappings values(?,?,?)',('alice','/old/users/alice','/new/users/alice'))
        self.store=Mock();self.store.remote_root.return_value='/new/users/alice';self.store.connect.return_value=self.db
        self.recipe={'id':'test','package_sha256':['b'*64],'assembly':{'output_sha256':'b'*64,'compatible_m100_sha256':[]}}
        self.package={'owner':'alice','data':{'sha256':'a'*64,'filename':'APP'}}
        self.reply={'state':'PREPARED','remote_path':'/old/users/alice/prepared-packages/abc/package.app.tar.gz',
                    'sha256':'b'*64,'source_sha256':'a'*64,'original_modified':False,'size':10,'changed_members':[]}
        self.registry=Mock();self.registry.ssh.python.side_effect=lambda *a,**k:json.dumps(self.reply)
    def call(self):
        with patch.object(assembly,'select',return_value=self.recipe),patch.object(assembly,'sensor_match',return_value=True),patch.object(assembly,'identity',return_value='abc'):
            return assembly.prepare({'project_root':str(self.project),'remote_users_root':'/new/users'},self.store,self.registry,'alice',self.package,{})
    def test_owner_mapping_translates_receipt_without_changing_original(self):
        _,receipt=self.call();self.assertEqual('/new/users/alice/prepared-packages/abc/package.app.tar.gz',receipt['remote_path'])
        self.assertTrue(self.reply['remote_path'].startswith('/old/'))
    def test_other_owner_mapping_does_not_authorize(self):
        self.db.execute('update storage_mappings set owner=?',('bob',))
        with self.assertRaises(assembly.GatewayError):self.call()
    def test_mapping_does_not_bypass_hash(self):
        self.reply['sha256']='c'*64
        with self.assertRaises(assembly.GatewayError):self.call()
    def test_unregistered_old_path_is_rejected(self):
        self.reply['remote_path']='/unknown/package.app.tar.gz'
        with self.assertRaises(assembly.GatewayError):self.call()
    def test_same_path_needs_no_mapping(self):
        self.reply['remote_path']='/new/users/alice/prepared-packages/abc/package.app.tar.gz'
        self.call()
if __name__=='__main__':unittest.main(verbosity=2)
