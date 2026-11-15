import base64
import ast
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from bench_gateway.core import Store
from bench_gateway.m100_build import build, BuildError, PACKAGE, OUTPUT, SENSORS


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'state');self.user='ou_compile_a'
        self.store.allow(self.user)
        self.cfg={'project_root':str(Path(__file__).resolve().parents[1]),'m100_build_enabled':True,'remote_users_root':'/data/users'}
        self.package={'owner':self.user,'data':{'sha256':PACKAGE}}
        self.dependencies={k:{'owner':self.user,'data':{'sha256':v}} for k,v in SENSORS.items()}
        self.ssh=Mock();self.registry=SimpleNamespace(ssh=self.ssh,remote_file=Mock(),record=Mock())

    def call(self,**changes):
        return build(self.cfg,self.store,self.registry,self.user,self.package,dependencies=self.dependencies,**changes)

    def test_unknown_release_and_foreign_owner_never_start_a_build(self):
        for package in ({'owner':self.user,'data':{'sha256':'a'*64}},{'owner':'ou_other','data':{'sha256':PACKAGE}}):
            with self.assertRaises(BuildError) as error:
                build(self.cfg,self.store,self.registry,self.user,package,dependencies=self.dependencies)
            self.assertEqual('M100_RECIPE_MISMATCH',error.exception.code)
        self.ssh.python.assert_not_called()

    def test_unreviewed_sensor_pair_stops_without_compilation(self):
        self.dependencies['lidar_so']['owner']='ou_other'
        with self.assertRaises(BuildError) as error:self.call()
        self.assertEqual('M100_SENSOR_UNVERIFIED',error.exception.code);self.ssh.python.assert_not_called()

    def test_disabled_and_insufficient_budget_never_contact_workstation(self):
        self.cfg['m100_build_enabled']=False
        with self.assertRaises(BuildError):self.call()
        self.cfg['m100_build_enabled']=True
        with self.assertRaises(BuildError):self.call(seconds=20)
        self.ssh.python.assert_not_called()

    def respond(self,state='BUILT',wrong_path=False,wrong_hash=False):
        def response(code,args=(),**kwargs):
            self.assertEqual((),args,'large build requests must not enter Windows argv')
            statement=ast.parse(code).body[1]
            encoded=ast.literal_eval(statement.value)[1]
            value=json.loads(base64.b64decode(encoded));self.request=value
            path=value['owner_root']+'/builds/m100-'+value['key']+'/artifact/liburoad_m100.so'
            return json.dumps({'state':state,'path':'/other/file.so' if wrong_path else path,
                               'sha256':'0'*64 if wrong_hash else OUTPUT,'size':101756152,'elapsed_seconds':111})
        self.ssh.python.side_effect=response
        self.registry.record.return_value={'id':'built','owner':self.user,'data':{'sha256':OUTPUT}}

    def test_validated_output_is_registered_only_to_owner_and_repeated_recipe_is_stable(self):
        self.respond();asset,receipt=self.call();first=self.request['key']
        self.assertEqual('built',asset['id']);self.assertIn('input_traj',receipt['absent_optional_outputs'])
        self.assertEqual(300,self.request['build_seconds'])
        self.call();self.assertEqual(first,self.request['key'])
        self.assertEqual(self.user,self.registry.record.call_args.args[0])

    def test_bad_receipt_never_registers_an_output(self):
        for options in ({'wrong_path':True},{'wrong_hash':True}):
            self.respond(**options)
            with self.assertRaises(BuildError):self.call()
        self.registry.record.assert_not_called()

    def test_build_failure_and_transport_loss_are_not_retried(self):
        self.respond('BUILD_FAILED')
        with self.assertRaises(BuildError) as error:self.call()
        self.assertEqual('M100_BUILD_FAILED',error.exception.code)
        self.assertEqual(1,self.ssh.python.call_count)
        self.ssh.reset_mock();self.ssh.python.side_effect=ConnectionError('private secret')
        with self.assertRaises(BuildError) as error:self.call()
        self.assertEqual('M100_BUILD_UNKNOWN',error.exception.code)
        self.assertNotIn('secret',str(error.exception));self.assertEqual(1,self.ssh.python.call_count)

    def test_build_identity_binds_exact_app_and_material_context(self):
        self.respond();self.call(build_context='task-A');a=self.request['key']
        self.call(build_context='task-B');b=self.request['key'];self.assertNotEqual(a,b)
        self.call(build_context='task-A');self.assertEqual(a,self.request['key'])
        from bench_gateway.recipes import load
        self.package['data']['sha256']=load(self.cfg)[0]['package_sha256'][1]
        self.call(build_context='task-A');self.assertNotEqual(a,self.request['key'])

    def test_sensor_candidates_are_not_rejected_by_old_hash_whitelist(self):
        self.respond();self.dependencies['lidar_so']['data']['sha256']='1'*64
        self.call(build_context='task-new-sensors')
        self.assertEqual('1'*64,self.request['identity']['sensors']['lidar_so'])

    def test_new_package_profile_has_no_old_expected_output_hash(self):
        import shutil
        from bench_gateway.recipes import load
        from bench_gateway.m100_profiles import resolve
        original=Path(self.cfg['project_root']);recipe=load(self.cfg)[0]
        root=self.root/'program';root.mkdir()
        for f in ['config/m100-conan-settings.yml','bench_gateway/m100_build_remote.py','vendor/build_uroad_eval.py']:
            p=root/f;p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(original/f,p)
        profile={'id':'new-app-compile','profile':'schu','build_mode':'offline-eval-v1','package_sha256':['a'*64],'build':recipe['build']}
        (root/'config/m100-build-profiles.json').write_text(json.dumps({'schema_version':1,'profiles':[profile]}))
        self.cfg['project_root']=str(root);self.package['data']['sha256']='a'*64
        self.respond();old=self.ssh.python.side_effect
        def reply(code,args=(),**kw):
            data=json.loads(old(code,args,**kw));data['sha256']='1'*64;return json.dumps(data)
        self.ssh.python.side_effect=reply
        self.registry.record.side_effect=lambda user,kind,name,data:dict(id='new-built',owner=user,data=data)
        asset,receipt=self.call(build_context='new-package')
        self.assertIsNone(self.request['expected_output']);self.assertEqual('1'*64,asset['data']['sha256'])
        self.assertEqual('a'*64,self.request['identity']['package_sha256'])
