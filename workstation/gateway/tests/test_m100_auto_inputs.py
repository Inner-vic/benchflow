import hashlib,io,json,tarfile,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from bench_gateway import m100_auto_inputs as inputs


class AutoInputsTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name)/'owner'
        self.store=SimpleNamespace(require_user=Mock(),remote_root=lambda *a:str(self.root))
        self.cfg={'workstation_local_execution':True,'remote_users_root':'/unused',
                  'livc_app_base_url':'https://artifactory.ep.chehejia.com/artifactory/xcu-acore-test-local/Acore/Civu/Release/app/'}
        self.pkg={'owner':'ou_test','data':{'sha256':'a'*64,'filename':'CCU_LIVC_AVM-release-SW0075_ULTRA_ID0111.20261027-2880.app.tar.gz'}}
        self.commit='b'*40
        self.recipe=('class Recipe:\n name="uroad_m100"\n branch="'+self.commit+'"\n'
            ' def source(self):\n  self.run(f"git clone https://gitlab.chehejia.com/livc-acore/chassis/uroad.git && git checkout {self.branch}")\n'
            ' def requirements(self):\n  self.requires("infer_expert_sdk/old@livc/stable")\n  self.requires("function-message/old@ad/release")\n'
            ' def configure(self):\n  self._cmake.definitions["WITH_EDS"] = True\n').encode()
        self.document={'version':'0.4','profile_host':'arch=armv8\nos=Linux\ncompiler.version=13.2\n','graph_lock':{'nodes':{
            '0':{'ref':'uroad_m100/3.1.5@livc/stable#'+'c'*32,'requires':['1','2'],'package_id':'old-root','prev':'old'},
            '1':{'ref':'infer_expert_sdk/0.1.89-rc2@livc/stable#'+'d'*32,'package_id':'sdk-pid','prev':'sdk-prev'},
            '2':{'ref':'function-message/v0.0.335@ad/release#'+'e'*32,'package_id':'msg-pid','prev':'msg-prev'},
            '99':{'ref':'unrelated/1.0@x/y#'+'f'*32}}}}
        archive=io.BytesIO()
        with tarfile.open(fileobj=archive,mode='w:gz') as tar:
            for name,body in [('uroad-commit/CMakeLists.txt',b'cmake_minimum_required(VERSION 3.10)'),('uroad-commit/src/main.cpp',b'// source fixture')]:
                item=tarfile.TarInfo(name);item.size=len(body);tar.addfile(item,io.BytesIO(body))
        self.archive=archive.getvalue();self.urls=[]
    def fetch(self,url,headers,limit,timeout):
        self.urls.append(url)
        if url.endswith('.lock'):return json.dumps(self.document).encode()
        if url.endswith('conanfile.py'):return self.recipe
        if url.endswith('conanmanifest.txt'):return ('0\nconanfile.py: '+hashlib.md5(self.recipe).hexdigest()+'\n').encode()
        if '/commits/' in url:return json.dumps({'id':self.commit}).encode()
        if 'archive.tar.gz' in url:return self.archive
        raise AssertionError('unexpected URL')
    def prepare(self,flag,**kwargs):
        with patch.object(inputs,'load_authorization',return_value='fixture-auth'),patch.object(inputs,'source_token',return_value='fixture-token'):
            return inputs.prepare(self.cfg,self.store,'ou_test',self.pkg,fetch=self.fetch,eval_dump=flag,**kwargs)
    def test_per_mode_inputs_preserve_exact_app_pins_and_legacy_files(self):
        root=self.root/'build-inputs'/('a'*64);root.mkdir(parents=True);(root/'recipe.patch').write_text('legacy frozen patch')
        on=self.prepare(True);off=self.prepare(False)
        self.assertNotEqual(on['source'],off['source']);self.assertNotEqual(on['source_snapshot_sha256'],off['source_snapshot_sha256'])
        for flag,result in [(True,on),(False,off)]:
            self.assertIs(result['eval_dump'],flag)
            body=(Path(result['source'])/'conanfile.py').read_text()
            self.assertIn('"WITH_EVAL_DUMP"] = '+str(flag),body)
            self.assertIn('infer_expert_sdk/0.1.89-rc2@livc/stable',body)
            self.assertIn('function-message/v0.0.335@ad/release',body)
            lock=json.loads((Path(result['source'])/'build/conan.lock').read_text())
            self.assertNotIn('99',lock['graph_lock']['nodes']);self.assertNotIn('package_id',lock['graph_lock']['nodes']['0'])
            self.assertEqual(self.document['graph_lock']['nodes']['1'],lock['graph_lock']['nodes']['1'])
        self.assertEqual('legacy frozen patch',(root/'recipe.patch').read_text())
        self.urls.clear();self.assertEqual(off['source_snapshot_sha256'],self.prepare(False)['source_snapshot_sha256']);self.assertEqual([],self.urls)
    def test_mismatched_build_link_never_fetches(self):
        with self.assertRaises(inputs.InputError) as error:self.prepare(False,build_reference='https://ci.ep.chehejia.com/job/build/9999/')
        self.assertEqual('M100_BUILD_REFERENCE_MISMATCH',error.exception.code);self.assertEqual([],self.urls)
    def test_build_link_rejects_embedded_credentials_and_other_ports(self):
        for url in ('https://u:p@ci.ep.chehejia.com/job/build/2880/','https://ci.ep.chehejia.com:8443/job/build/2880/'):
            with self.assertRaises(inputs.InputError):self.prepare(False,build_reference=url)
        self.assertEqual([],self.urls)
    def test_cached_mode_or_source_path_cannot_be_substituted(self):
        result=self.prepare(False);marker=Path(result['source']).parent/'inputs-replay-only-v2.json';old=marker.read_text()
        for changes in ({'eval_dump':True},{'source':'/tmp/other-source'}):
            data=json.loads(old);data.update(changes);marker.write_text(json.dumps(data))
            with self.assertRaises(ValueError):self.prepare(False)
        marker.write_text(old)
    def test_only_verified_assembled_app_can_recover_original_filename(self):
        self.pkg['data'].update(filename=self.pkg['data']['filename']+'-prepared',release_assembly={'source_sha256':'f'*64})
        with patch('bench_gateway.recipes.select',return_value={'assembly':{'source_sha256':'f'*64}}):
            self.assertEqual('READY',self.prepare(False)['state'])
    def test_unknown_source_host_or_unpinned_commit_is_rejected(self):
        self.recipe=self.recipe.replace(b'gitlab.chehejia.com',b'untrusted.example.com')
        with self.assertRaises(inputs.InputError):self.prepare(False)
    def test_changed_cached_source_lock_is_rejected(self):
        result=self.prepare(False);(Path(result['source'])/'build/conan.lock').write_text('{}')
        with self.assertRaises(ValueError):self.prepare(False)
    def test_source_archive_links_are_rejected(self):
        archive=io.BytesIO()
        with tarfile.open(fileobj=archive,mode='w:gz') as tar:
            item=tarfile.TarInfo('root/link');item.type=tarfile.SYMTYPE;item.linkname='/etc/passwd';tar.addfile(item)
        with self.assertRaises(ValueError):inputs.extract_source(archive.getvalue(),self.root/'source')
        self.assertFalse((self.root/'source').exists())


if __name__=='__main__':unittest.main()
